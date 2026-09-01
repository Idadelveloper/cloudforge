"""Data access layer for the CloudForge IoT telemetry backend.

Every AWS interaction is hidden behind the :class:`TelemetryStore` interface so
that the HTTP layer can be tested with :class:`InMemoryTelemetryStore` without
any network access or a running LocalStack instance.
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

LOGGER = logging.getLogger("iot_telemetry.storage")

DEFAULT_REGION = "us-east-1"
DEFAULT_DEVICES_TABLE = "iot-devices"
DEFAULT_READINGS_TABLE = "iot-readings"
DEFAULT_TOPIC_NAME = "iot-telemetry-alerts"


def aws_region() -> str:
    """Region used for every AWS client."""
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or DEFAULT_REGION


def aws_endpoint_url() -> Optional[str]:
    """Endpoint override (LocalStack) or ``None`` for real AWS."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource():
    """Create a DynamoDB resource honouring AWS_ENDPOINT_URL."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def sns_client():
    """Create an SNS client honouring AWS_ENDPOINT_URL."""
    return boto3.client(
        "sns",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def devices_table_name() -> str:
    """Name of the device registry table."""
    return os.environ.get("DEVICES_TABLE", DEFAULT_DEVICES_TABLE)


def readings_table_name() -> str:
    """Name of the readings table."""
    return os.environ.get("READINGS_TABLE", DEFAULT_READINGS_TABLE)


def alerts_topic_name() -> str:
    """Name of the SNS alerts topic."""
    return os.environ.get("ALERTS_TOPIC_NAME", DEFAULT_TOPIC_NAME)


def alerts_topic_arn() -> str:
    """Optional pre-resolved SNS topic ARN."""
    return os.environ.get("ALERTS_TOPIC_ARN", "")


def to_dynamo(value: Any) -> Any:
    """Recursively convert floats to Decimal for DynamoDB writes."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: to_dynamo(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(item) for item in value]
    return value


def from_dynamo(value: Any) -> Any:
    """Recursively convert Decimal values back to floats for JSON output."""
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: from_dynamo(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [from_dynamo(item) for item in value]
    return value


class TelemetryStore:
    """Interface implemented by the DynamoDB and in-memory stores."""

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Return a device record or ``None``."""
        raise NotImplementedError

    def put_device(self, device: Dict[str, Any]) -> Dict[str, Any]:
        """Create or replace a device record."""
        raise NotImplementedError

    def list_devices(self) -> List[Dict[str, Any]]:
        """Return every device record."""
        raise NotImplementedError

    def put_reading(self, reading: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a temperature reading."""
        raise NotImplementedError

    def query_readings(
        self,
        device_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return readings for a device ordered by timestamp."""
        raise NotImplementedError

    def publish_alert(self, subject: str, message: Dict[str, Any]) -> bool:
        """Publish an alert; return ``True`` when the publish succeeded."""
        raise NotImplementedError


class DynamoTelemetryStore(TelemetryStore):
    """DynamoDB + SNS implementation of :class:`TelemetryStore`."""

    def __init__(self, resource: Any = None, sns: Any = None) -> None:
        self._resource = resource
        self._sns = sns
        self._topic_arn: Optional[str] = None

    @property
    def resource(self) -> Any:
        """Lazily created DynamoDB resource."""
        if self._resource is None:
            self._resource = dynamodb_resource()
        return self._resource

    @property
    def sns(self) -> Any:
        """Lazily created SNS client."""
        if self._sns is None:
            self._sns = sns_client()
        return self._sns

    @property
    def devices_table(self) -> Any:
        """Device registry table handle."""
        return self.resource.Table(devices_table_name())

    @property
    def readings_table(self) -> Any:
        """Readings table handle."""
        return self.resource.Table(readings_table_name())

    def topic_arn(self) -> Optional[str]:
        """Resolve the SNS topic ARN, creating the topic if necessary."""
        if self._topic_arn:
            return self._topic_arn
        configured = alerts_topic_arn()
        if configured:
            self._topic_arn = configured
            return self._topic_arn
        try:
            response = self.sns.create_topic(Name=alerts_topic_name())
            self._topic_arn = response.get("TopicArn")
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.error("unable to resolve SNS topic %s: %s", alerts_topic_name(), exc)
            self._topic_arn = None
        return self._topic_arn

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Return a device record or ``None``."""
        response = self.devices_table.get_item(Key={"device_id": device_id})
        item = response.get("Item")
        if not item:
            return None
        return from_dynamo(item)

    def put_device(self, device: Dict[str, Any]) -> Dict[str, Any]:
        """Create or replace a device record."""
        self.devices_table.put_item(Item=to_dynamo(device))
        return device

    def list_devices(self) -> List[Dict[str, Any]]:
        """Scan the registry table."""
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {}
        while True:
            response = self.devices_table.scan(**kwargs)
            items.extend(from_dynamo(item) for item in response.get("Items", []))
            token = response.get("LastEvaluatedKey")
            if not token:
                break
            kwargs["ExclusiveStartKey"] = token
        return items

    def put_reading(self, reading: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a temperature reading."""
        self.readings_table.put_item(Item=to_dynamo(reading))
        return reading

    def query_readings(
        self,
        device_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Query readings for a device within an optional timestamp range."""
        condition = Key("device_id").eq(device_id)
        if start and end:
            condition = condition & Key("timestamp").between(start, end)
        elif start:
            condition = condition & Key("timestamp").gte(start)
        elif end:
            condition = condition & Key("timestamp").lte(end)

        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": condition,
            "ScanIndexForward": True,
        }
        items: List[Dict[str, Any]] = []
        while True:
            response = self.readings_table.query(**kwargs)
            items.extend(from_dynamo(item) for item in response.get("Items", []))
            token = response.get("LastEvaluatedKey")
            if not token:
                break
            if limit is not None and len(items) >= limit:
                break
            kwargs["ExclusiveStartKey"] = token
        items.sort(key=lambda item: str(item.get("timestamp", "")))
        if limit is not None:
            items = items[:limit]
        return items

    def publish_alert(self, subject: str, message: Dict[str, Any]) -> bool:
        """Publish an alert message to the SNS topic."""
        arn = self.topic_arn()
        if not arn:
            LOGGER.error("no SNS topic ARN available; alert dropped")
            return False
        try:
            self.sns.publish(
                TopicArn=arn,
                Subject=subject[:100],
                Message=json.dumps(message),
            )
            return True
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.error("failed to publish SNS alert: %s", exc)
            return False


class InMemoryTelemetryStore(TelemetryStore):
    """In-memory store used by the test-suite and local development."""

    def __init__(self) -> None:
        self.devices: Dict[str, Dict[str, Any]] = {}
        self.readings: Dict[str, List[Dict[str, Any]]] = {}
        self.published: List[Dict[str, Any]] = []
        self.publish_should_fail = False

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Return a copy of the device record or ``None``."""
        device = self.devices.get(device_id)
        return dict(device) if device else None

    def put_device(self, device: Dict[str, Any]) -> Dict[str, Any]:
        """Create or replace a device record."""
        self.devices[str(device["device_id"])] = dict(device)
        return device

    def list_devices(self) -> List[Dict[str, Any]]:
        """Return every stored device."""
        return [dict(device) for device in self.devices.values()]

    def put_reading(self, reading: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a reading in memory."""
        bucket = self.readings.setdefault(str(reading["device_id"]), [])
        bucket.append(dict(reading))
        bucket.sort(key=lambda item: str(item.get("timestamp", "")))
        return reading

    def query_readings(
        self,
        device_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return readings within the optional timestamp range."""
        items = [dict(item) for item in self.readings.get(device_id, [])]
        if start:
            items = [item for item in items if str(item.get("timestamp", "")) >= start]
        if end:
            items = [item for item in items if str(item.get("timestamp", "")) <= end]
        items.sort(key=lambda item: str(item.get("timestamp", "")))
        if limit is not None:
            items = items[:limit]
        return items

    def publish_alert(self, subject: str, message: Dict[str, Any]) -> bool:
        """Record the alert instead of calling SNS."""
        if self.publish_should_fail:
            return False
        self.published.append({"subject": subject, "message": dict(message)})
        return True


def create_store() -> TelemetryStore:
    """Factory returning the AWS backed store."""
    return DynamoTelemetryStore()
