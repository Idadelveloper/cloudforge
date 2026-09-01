"""Data access layer for the IoT telemetry backend.

Wraps DynamoDB (device registry + readings table) and SNS (alert topic) behind
a tiny repository interface, plus an in-memory implementation used by tests and
local development.
"""

import abc
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_DEVICES_TABLE = "iot-devices"
DEFAULT_READINGS_TABLE = "iot-readings"
DEFAULT_ALERTS_TOPIC = "iot-temperature-alerts"


class StorageError(Exception):
    """Raised when the backing AWS store cannot serve a request."""


# --------------------------------------------------------------------------- #
# Configuration helpers
# --------------------------------------------------------------------------- #

def aws_region() -> str:
    return (
        os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or DEFAULT_REGION
    )


def aws_endpoint_url() -> Optional[str]:
    """LocalStack (or any custom) endpoint, or None for real AWS."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def devices_table_name() -> str:
    return os.environ.get("DEVICES_TABLE", DEFAULT_DEVICES_TABLE)


def readings_table_name() -> str:
    return os.environ.get("READINGS_TABLE", DEFAULT_READINGS_TABLE)


def alerts_topic_name() -> str:
    return os.environ.get("ALERTS_TOPIC_NAME", DEFAULT_ALERTS_TOPIC)


def alerts_topic_arn() -> Optional[str]:
    return os.environ.get("ALERTS_TOPIC_ARN") or None


def dynamodb_resource():
    """DynamoDB resource honouring AWS_ENDPOINT_URL / region env vars."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def sns_client():
    """SNS client honouring AWS_ENDPOINT_URL / region env vars."""
    return boto3.client(
        "sns",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


# --------------------------------------------------------------------------- #
# (De)serialisation helpers
# --------------------------------------------------------------------------- #

def to_decimal(value: Any) -> Any:
    """Recursively convert floats to Decimal for DynamoDB writes."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, dict):
        return {key: to_decimal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_decimal(item) for item in value]
    return value


def to_plain(value: Any) -> Any:
    """Recursively convert DynamoDB Decimals to int/float for JSON output."""
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        as_float = float(value)
        if as_float.is_integer():
            return int(as_float)
        return as_float
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    return value


# --------------------------------------------------------------------------- #
# Repository interface
# --------------------------------------------------------------------------- #

class TelemetryRepository(abc.ABC):
    """Persistence + notification operations used by the HTTP layer."""

    @abc.abstractmethod
    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Return the device registry item or None."""

    @abc.abstractmethod
    def put_device(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Store a device registry item."""

    @abc.abstractmethod
    def list_devices(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return registered devices."""

    @abc.abstractmethod
    def update_threshold(
        self, device_id: str, threshold: float, updated_at: str
    ) -> Optional[Dict[str, Any]]:
        """Update the device threshold and return the new item."""

    @abc.abstractmethod
    def put_reading(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Store a temperature reading."""

    @abc.abstractmethod
    def query_readings(
        self,
        device_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return readings for a device ordered by timestamp."""

    @abc.abstractmethod
    def publish_alert(self, message: Dict[str, Any]) -> str:
        """Publish an alert and return the provider message id."""


# --------------------------------------------------------------------------- #
# DynamoDB / SNS implementation
# --------------------------------------------------------------------------- #

class DynamoTelemetryRepository(TelemetryRepository):
    """Repository backed by two DynamoDB tables and one SNS topic."""

    def __init__(
        self,
        resource: Any = None,
        sns: Any = None,
        devices_table: Optional[str] = None,
        readings_table: Optional[str] = None,
        topic_arn: Optional[str] = None,
    ) -> None:
        self._resource = resource
        self._sns = sns
        self._devices_table_name = devices_table or devices_table_name()
        self._readings_table_name = readings_table or readings_table_name()
        self._topic_arn = topic_arn or alerts_topic_arn()

    # -- lazy clients ------------------------------------------------------ #

    def _dynamodb(self) -> Any:
        if self._resource is None:
            self._resource = dynamodb_resource()
        return self._resource

    def _devices(self) -> Any:
        return self._dynamodb().Table(self._devices_table_name)

    def _readings(self) -> Any:
        return self._dynamodb().Table(self._readings_table_name)

    def _sns_client(self) -> Any:
        if self._sns is None:
            self._sns = sns_client()
        return self._sns

    def _topic(self) -> str:
        if not self._topic_arn:
            response = self._sns_client().create_topic(Name=alerts_topic_name())
            self._topic_arn = response["TopicArn"]
        return self._topic_arn

    # -- devices ----------------------------------------------------------- #

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self._devices().get_item(Key={"device_id": device_id})
        except Exception as exc:  # noqa: BLE001 - surfaced as 503
            raise StorageError("failed to read device {0}: {1}".format(device_id, exc)) from exc
        item = response.get("Item")
        if not item:
            return None
        return to_plain(item)

    def put_device(self, item: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self._devices().put_item(Item=to_decimal(item))
        except Exception as exc:  # noqa: BLE001
            raise StorageError("failed to write device: {0}".format(exc)) from exc
        return item

    def list_devices(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {}
        if limit:
            kwargs["Limit"] = int(limit)
        try:
            response = self._devices().scan(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise StorageError("failed to list devices: {0}".format(exc)) from exc
        items = [to_plain(entry) for entry in response.get("Items", [])]
        items.sort(key=lambda entry: str(entry.get("device_id", "")))
        if limit:
            return items[: int(limit)]
        return items

    def update_threshold(
        self, device_id: str, threshold: float, updated_at: str
    ) -> Optional[Dict[str, Any]]:
        try:
            response = self._devices().update_item(
                Key={"device_id": device_id},
                UpdateExpression="SET threshold_celsius = :t, updated_at = :u",
                ExpressionAttributeValues={
                    ":t": to_decimal(float(threshold)),
                    ":u": updated_at,
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:  # noqa: BLE001
            raise StorageError(
                "failed to update threshold for {0}: {1}".format(device_id, exc)
            ) from exc
        attributes = response.get("Attributes")
        if not attributes:
            return None
        return to_plain(attributes)

    # -- readings ---------------------------------------------------------- #

    def put_reading(self, item: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self._readings().put_item(Item=to_decimal(item))
        except Exception as exc:  # noqa: BLE001
            raise StorageError("failed to write reading: {0}".format(exc)) from exc
        return item

    def query_readings(
        self,
        device_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
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
        if limit:
            kwargs["Limit"] = int(limit)

        items: List[Dict[str, Any]] = []
        while True:
            try:
                response = self._readings().query(**kwargs)
            except Exception as exc:  # noqa: BLE001
                raise StorageError(
                    "failed to query readings for {0}: {1}".format(device_id, exc)
                ) from exc
            items.extend(to_plain(entry) for entry in response.get("Items", []))
            next_key = response.get("LastEvaluatedKey")
            if not next_key:
                break
            if limit and len(items) >= int(limit):
                break
            kwargs["ExclusiveStartKey"] = next_key

        items.sort(key=lambda entry: str(entry.get("timestamp", "")))
        if limit:
            return items[: int(limit)]
        return items

    # -- alerts ------------------------------------------------------------ #

    def publish_alert(self, message: Dict[str, Any]) -> str:
        payload = json.dumps(message, default=str, sort_keys=True)
        try:
            response = self._sns_client().publish(
                TopicArn=self._topic(),
                Subject="IoT temperature alert",
                Message=payload,
            )
        except Exception as exc:  # noqa: BLE001
            raise StorageError("failed to publish alert: {0}".format(exc)) from exc
        return str(response.get("MessageId", ""))


# --------------------------------------------------------------------------- #
# In-memory implementation (tests / local runs)
# --------------------------------------------------------------------------- #

class InMemoryTelemetryRepository(TelemetryRepository):
    """Dependency-free repository used by tests and offline development."""

    def __init__(self) -> None:
        self.devices: Dict[str, Dict[str, Any]] = {}
        self.readings: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.published_alerts: List[Dict[str, Any]] = []

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        item = self.devices.get(device_id)
        return dict(item) if item is not None else None

    def put_device(self, item: Dict[str, Any]) -> Dict[str, Any]:
        self.devices[str(item["device_id"])] = dict(item)
        return dict(item)

    def list_devices(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        items = [dict(entry) for entry in self.devices.values()]
        items.sort(key=lambda entry: str(entry.get("device_id", "")))
        if limit:
            return items[: int(limit)]
        return items

    def update_threshold(
        self, device_id: str, threshold: float, updated_at: str
    ) -> Optional[Dict[str, Any]]:
        item = self.devices.get(device_id)
        if item is None:
            return None
        item["threshold_celsius"] = float(threshold)
        item["updated_at"] = updated_at
        return dict(item)

    def put_reading(self, item: Dict[str, Any]) -> Dict[str, Any]:
        device_id = str(item["device_id"])
        self.readings.setdefault(device_id, {})[str(item["timestamp"])] = dict(item)
        return dict(item)

    def query_readings(
        self,
        device_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        items = [dict(entry) for entry in self.readings.get(device_id, {}).values()]
        if start:
            items = [entry for entry in items if str(entry.get("timestamp", "")) >= start]
        if end:
            items = [entry for entry in items if str(entry.get("timestamp", "")) <= end]
        items.sort(key=lambda entry: str(entry.get("timestamp", "")))
        if limit:
            return items[: int(limit)]
        return items

    def publish_alert(self, message: Dict[str, Any]) -> str:
        self.published_alerts.append(dict(message))
        return "in-memory-{0}".format(len(self.published_alerts))
