"""Storage and messaging layer for the event registration service.

The application only depends on the small :class:`Repository` and
:class:`Publisher` interfaces.  Production uses the DynamoDB / SQS backed
implementations; tests can inject the in-memory ones.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_EVENTS_TABLE = "events"
DEFAULT_REGISTRATIONS_TABLE = "registrations"
DEFAULT_QUEUE_NAME = "registration-events"
REGISTRATION_EVENT_TYPE = "registration.created"
STATUS_CONFIRMED = "confirmed"


def aws_region() -> str:
    """Return the AWS region to use for every client."""
    return os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def aws_endpoint_url() -> Optional[str]:
    """Return the LocalStack/custom endpoint URL when configured."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource():
    """Create a DynamoDB resource honouring AWS_ENDPOINT_URL."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def sqs_client():
    """Create an SQS client honouring AWS_ENDPOINT_URL."""
    return boto3.client(
        "sqs",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class StorageError(Exception):
    """Base class for storage level failures."""


class EventNotFound(StorageError):
    """Raised when an event id is unknown."""

    def __init__(self, event_id: str) -> None:
        super().__init__("event '%s' does not exist" % event_id)
        self.event_id = event_id


class RegistrationNotFound(StorageError):
    """Raised when a registration id is unknown."""

    def __init__(self, registration_id: str) -> None:
        super().__init__("registration '%s' does not exist" % registration_id)
        self.registration_id = registration_id


class EventFull(StorageError):
    """Raised when an event has reached its capacity."""

    def __init__(self, event_id: str) -> None:
        super().__init__("event '%s' is full" % event_id)
        self.event_id = event_id


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, Decimal, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return default


def normalise_event(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw DynamoDB event item into plain Python types."""
    return {
        "event_id": str(item.get("event_id", "")),
        "title": str(item.get("title", "")),
        "date": str(item.get("date", "")),
        "capacity": _to_int(item.get("capacity")),
        "registered_count": _to_int(item.get("registered_count")),
        "created_at": str(item.get("created_at", "")),
    }


def normalise_registration(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw DynamoDB registration item into plain Python types."""
    return {
        "registration_id": str(item.get("registration_id", "")),
        "event_id": str(item.get("event_id", "")),
        "attendee_name": str(item.get("attendee_name", "")),
        "attendee_email": str(item.get("attendee_email", "")),
        "status": str(item.get("status", STATUS_CONFIRMED)),
        "created_at": str(item.get("created_at", "")),
    }


def new_event_record(title: str, date: str, capacity: int) -> Dict[str, Any]:
    """Build a fresh event record."""
    return {
        "event_id": str(uuid.uuid4()),
        "title": title,
        "date": date,
        "capacity": int(capacity),
        "registered_count": 0,
        "created_at": utc_now(),
    }


def new_registration_record(event_id: str, attendee_name: str, attendee_email: str) -> Dict[str, Any]:
    """Build a fresh registration record."""
    return {
        "event_id": event_id,
        "registration_id": str(uuid.uuid4()),
        "attendee_name": attendee_name,
        "attendee_email": attendee_email.strip().lower(),
        "status": STATUS_CONFIRMED,
        "created_at": utc_now(),
    }


def build_registration_message(event: Dict[str, Any], registration: Dict[str, Any]) -> Dict[str, Any]:
    """Build the JSON payload published to SQS after a registration."""
    return {
        "event_type": REGISTRATION_EVENT_TYPE,
        "registration_id": registration["registration_id"],
        "event_id": event.get("event_id", registration["event_id"]),
        "event_title": event.get("title", ""),
        "attendee_name": registration["attendee_name"],
        "attendee_email": registration["attendee_email"],
        "occurred_at": utc_now(),
    }


class Repository:
    """Interface implemented by the storage backends."""

    def create_event(self, title: str, date: str, capacity: int) -> Dict[str, Any]:
        raise NotImplementedError

    def list_events(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def find_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def get_event(self, event_id: str) -> Dict[str, Any]:
        event = self.find_event(event_id)
        if event is None:
            raise EventNotFound(event_id)
        return event

    def reserve_capacity(self, event_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def release_capacity(self, event_id: str) -> None:
        raise NotImplementedError

    def create_registration(self, event_id: str, attendee_name: str, attendee_email: str) -> Dict[str, Any]:
        raise NotImplementedError

    def list_registrations(self, event_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def find_registration_by_email(self, event_id: str, attendee_email: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def find_registration(self, registration_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def get_registration(self, registration_id: str) -> Dict[str, Any]:
        registration = self.find_registration(registration_id)
        if registration is None:
            raise RegistrationNotFound(registration_id)
        return registration


class DynamoRepository(Repository):
    """DynamoDB backed repository."""

    def __init__(
        self,
        events_table_name: Optional[str] = None,
        registrations_table_name: Optional[str] = None,
        dynamodb: Any = None,
    ) -> None:
        self.events_table_name = events_table_name or os.environ.get(
            "EVENTS_TABLE", DEFAULT_EVENTS_TABLE
        )
        self.registrations_table_name = registrations_table_name or os.environ.get(
            "REGISTRATIONS_TABLE", DEFAULT_REGISTRATIONS_TABLE
        )
        self._dynamodb = dynamodb

    @property
    def dynamodb(self) -> Any:
        if self._dynamodb is None:
            self._dynamodb = dynamodb_resource()
        return self._dynamodb

    @property
    def events_table(self) -> Any:
        return self.dynamodb.Table(self.events_table_name)

    @property
    def registrations_table(self) -> Any:
        return self.dynamodb.Table(self.registrations_table_name)

    @staticmethod
    def _paginate(operation: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        params = dict(kwargs)
        while True:
            response = operation(**params)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            params["ExclusiveStartKey"] = last_key

    def create_event(self, title: str, date: str, capacity: int) -> Dict[str, Any]:
        record = new_event_record(title, date, capacity)
        self.events_table.put_item(
            Item=record,
            ConditionExpression="attribute_not_exists(event_id)",
        )
        return record

    def list_events(self) -> List[Dict[str, Any]]:
        items = self._paginate(self.events_table.scan)
        events = [normalise_event(item) for item in items]
        return sorted(events, key=lambda event: event["created_at"])

    def find_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        response = self.events_table.get_item(Key={"event_id": event_id})
        item = response.get("Item")
        return normalise_event(item) if item else None

    def reserve_capacity(self, event_id: str) -> Dict[str, Any]:
        table = self.events_table
        try:
            response = table.update_item(
                Key={"event_id": event_id},
                UpdateExpression="SET #rc = #rc + :inc",
                ConditionExpression="attribute_exists(event_id) AND #rc < #cap",
                ExpressionAttributeNames={"#rc": "registered_count", "#cap": "capacity"},
                ExpressionAttributeValues={":inc": 1},
                ReturnValues="ALL_NEW",
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException as exc:
            if self.find_event(event_id) is None:
                raise EventNotFound(event_id) from exc
            raise EventFull(event_id) from exc
        return normalise_event(response.get("Attributes", {}))

    def release_capacity(self, event_id: str) -> None:
        table = self.events_table
        try:
            table.update_item(
                Key={"event_id": event_id},
                UpdateExpression="SET #rc = #rc - :dec",
                ConditionExpression="attribute_exists(event_id) AND #rc > :zero",
                ExpressionAttributeNames={"#rc": "registered_count"},
                ExpressionAttributeValues={":dec": 1, ":zero": 0},
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            logger.warning("could not release capacity for event %s", event_id)

    def create_registration(self, event_id: str, attendee_name: str, attendee_email: str) -> Dict[str, Any]:
        record = new_registration_record(event_id, attendee_name, attendee_email)
        self.registrations_table.put_item(Item=record)
        return record

    def list_registrations(self, event_id: str) -> List[Dict[str, Any]]:
        items = self._paginate(
            self.registrations_table.query,
            KeyConditionExpression=Key("event_id").eq(event_id),
        )
        registrations = [normalise_registration(item) for item in items]
        return sorted(registrations, key=lambda reg: reg["created_at"])

    def find_registration_by_email(self, event_id: str, attendee_email: str) -> Optional[Dict[str, Any]]:
        email = attendee_email.strip().lower()
        items = self._paginate(
            self.registrations_table.query,
            KeyConditionExpression=Key("event_id").eq(event_id),
            FilterExpression=Attr("attendee_email").eq(email),
        )
        for item in items:
            registration = normalise_registration(item)
            if registration["attendee_email"] == email:
                return registration
        return None

    def find_registration(self, registration_id: str) -> Optional[Dict[str, Any]]:
        items = self._paginate(
            self.registrations_table.scan,
            FilterExpression=Attr("registration_id").eq(registration_id),
        )
        for item in items:
            registration = normalise_registration(item)
            if registration["registration_id"] == registration_id:
                return registration
        return None


class InMemoryRepository(Repository):
    """Dictionary backed repository used for local runs and tests."""

    def __init__(self) -> None:
        self._events: Dict[str, Dict[str, Any]] = {}
        self._registrations: Dict[str, Dict[str, Any]] = {}

    def create_event(self, title: str, date: str, capacity: int) -> Dict[str, Any]:
        record = new_event_record(title, date, capacity)
        self._events[record["event_id"]] = record
        return dict(record)

    def list_events(self) -> List[Dict[str, Any]]:
        events = [dict(event) for event in self._events.values()]
        return sorted(events, key=lambda event: event["created_at"])

    def find_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        event = self._events.get(event_id)
        return dict(event) if event else None

    def reserve_capacity(self, event_id: str) -> Dict[str, Any]:
        event = self._events.get(event_id)
        if event is None:
            raise EventNotFound(event_id)
        if int(event["registered_count"]) >= int(event["capacity"]):
            raise EventFull(event_id)
        event["registered_count"] = int(event["registered_count"]) + 1
        return dict(event)

    def release_capacity(self, event_id: str) -> None:
        event = self._events.get(event_id)
        if event and int(event["registered_count"]) > 0:
            event["registered_count"] = int(event["registered_count"]) - 1

    def create_registration(self, event_id: str, attendee_name: str, attendee_email: str) -> Dict[str, Any]:
        record = new_registration_record(event_id, attendee_name, attendee_email)
        self._registrations[record["registration_id"]] = record
        return dict(record)

    def list_registrations(self, event_id: str) -> List[Dict[str, Any]]:
        found = [dict(reg) for reg in self._registrations.values() if reg["event_id"] == event_id]
        return sorted(found, key=lambda reg: reg["created_at"])

    def find_registration_by_email(self, event_id: str, attendee_email: str) -> Optional[Dict[str, Any]]:
        email = attendee_email.strip().lower()
        for reg in self._registrations.values():
            if reg["event_id"] == event_id and reg["attendee_email"] == email:
                return dict(reg)
        return None

    def find_registration(self, registration_id: str) -> Optional[Dict[str, Any]]:
        registration = self._registrations.get(registration_id)
        return dict(registration) if registration else None


class Publisher:
    """Interface for publishing registration messages."""

    def publish(self, message: Dict[str, Any]) -> None:
        raise NotImplementedError


class SqsPublisher(Publisher):
    """Publishes registration messages as JSON to an SQS queue."""

    def __init__(
        self,
        queue_url: Optional[str] = None,
        queue_name: Optional[str] = None,
        client: Any = None,
    ) -> None:
        self._queue_url = queue_url if queue_url is not None else os.environ.get("REGISTRATION_QUEUE_URL", "")
        self.queue_name = queue_name or os.environ.get("REGISTRATION_QUEUE_NAME", DEFAULT_QUEUE_NAME)
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = sqs_client()
        return self._client

    def queue_url(self) -> str:
        """Return the queue URL, resolving it from the queue name if needed."""
        if not self._queue_url:
            response = self.client.get_queue_url(QueueName=self.queue_name)
            self._queue_url = response["QueueUrl"]
        return self._queue_url

    def publish(self, message: Dict[str, Any]) -> None:
        self.client.send_message(
            QueueUrl=self.queue_url(),
            MessageBody=json.dumps(message),
        )


class InMemoryPublisher(Publisher):
    """Collects published messages in a list (used by tests)."""

    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []
        self.queue_name = "in-memory"

    def publish(self, message: Dict[str, Any]) -> None:
        self.messages.append(message)
