"""Data access layer: DynamoDB repositories and the SQS publisher.

All AWS clients honour AWS_ENDPOINT_URL (LocalStack) and default to the
us-east-1 region. Resource names come from environment variables.
"""
import base64
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3

LOGGER = logging.getLogger(__name__)

DEFAULT_EVENTS_TABLE = "events"
DEFAULT_REGISTRATIONS_TABLE = "registrations"
DEFAULT_QUEUE_NAME = "registration-events"


class StorageError(Exception):
    """Base class for storage failures."""


class EventNotFoundError(StorageError):
    """Raised when an event does not exist."""


class EventFullError(StorageError):
    """Raised when an event has no remaining capacity."""


class DuplicateRegistrationError(StorageError):
    """Raised when an attendee email is already registered for an event."""


class InvalidCursorError(StorageError):
    """Raised when a pagination cursor cannot be decoded."""


def _aws_kwargs() -> Dict[str, Any]:
    return {
        "region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        "endpoint_url": os.environ.get("AWS_ENDPOINT_URL") or None,
    }


def dynamodb_resource():
    """Create a DynamoDB resource pointing at LocalStack when configured."""
    return boto3.resource("dynamodb", **_aws_kwargs())


def sqs_client():
    """Create an SQS client pointing at LocalStack when configured."""
    return boto3.client("sqs", **_aws_kwargs())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _encode_cursor(data: Dict[str, Any]) -> str:
    raw = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> Dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise InvalidCursorError("invalid pagination cursor") from exc
    if not isinstance(data, dict):
        raise InvalidCursorError("invalid pagination cursor")
    return data


def _cursor_offset(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    data = _decode_cursor(cursor)
    offset = data.get("offset")
    if not isinstance(offset, int) or offset < 0:
        raise InvalidCursorError("invalid pagination cursor")
    return offset


def _cursor_key(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    if not cursor:
        return None
    data = _decode_cursor(cursor)
    key = data.get("key")
    if not isinstance(key, dict) or not key:
        raise InvalidCursorError("invalid pagination cursor")
    return key


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    return value


def _normalize_item(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {key: _normalize_value(value) for key, value in (item or {}).items()}


class DynamoDBRepository:
    """Repository backed by two DynamoDB tables."""

    def __init__(self, dynamodb=None, events_table=None, registrations_table=None):
        self._dynamodb = dynamodb if dynamodb is not None else dynamodb_resource()
        self._events_name = events_table or os.environ.get(
            "EVENTS_TABLE", DEFAULT_EVENTS_TABLE
        )
        self._registrations_name = registrations_table or os.environ.get(
            "REGISTRATIONS_TABLE", DEFAULT_REGISTRATIONS_TABLE
        )

    @property
    def events_table(self):
        return self._dynamodb.Table(self._events_name)

    @property
    def registrations_table(self):
        return self._dynamodb.Table(self._registrations_name)

    @property
    def _condition_failed(self):
        return self._dynamodb.meta.client.exceptions.ConditionalCheckFailedException

    def create_event(self, title: str, date: str, capacity: int) -> Dict[str, Any]:
        item = {
            "event_id": str(uuid.uuid4()),
            "title": title,
            "date": date,
            "capacity": int(capacity),
            "registered_count": 0,
            "created_at": _now(),
        }
        self.events_table.put_item(Item=item)
        return dict(item)

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        response = self.events_table.get_item(Key={"event_id": event_id})
        item = response.get("Item")
        if not item:
            return None
        return _normalize_item(item)

    def list_events(
        self, limit: int = 100, cursor: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": int(limit)}
        start_key = _cursor_key(cursor)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = self.events_table.scan(**kwargs)
        items = [_normalize_item(item) for item in response.get("Items", [])]
        items.sort(key=lambda event: (event.get("created_at", ""), event.get("event_id", "")))
        last_key = response.get("LastEvaluatedKey")
        next_cursor = _encode_cursor({"key": last_key}) if last_key else None
        return items, next_cursor

    def list_registrations(
        self, event_id: str, limit: int = 100, cursor: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": "event_id = :eid",
            "ExpressionAttributeValues": {":eid": event_id},
            "Limit": int(limit),
        }
        start_key = _cursor_key(cursor)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = self.registrations_table.query(**kwargs)
        items = [_normalize_item(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        next_cursor = _encode_cursor({"key": last_key}) if last_key else None
        return items, next_cursor

    def _find_registration_by_email(self, event_id: str, email: str) -> Optional[Dict[str, Any]]:
        target = email.strip().lower()
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": "event_id = :eid",
            "ExpressionAttributeValues": {":eid": event_id},
        }
        while True:
            response = self.registrations_table.query(**kwargs)
            for item in response.get("Items", []):
                stored = str(item.get("attendee_email", "")).strip().lower()
                if stored == target:
                    return _normalize_item(item)
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return None
            kwargs["ExclusiveStartKey"] = last_key

    def _release_seat(self, event_id: str) -> None:
        try:
            self.events_table.update_item(
                Key={"event_id": event_id},
                UpdateExpression="SET #rc = #rc - :one",
                ConditionExpression="#rc > :zero",
                ExpressionAttributeNames={"#rc": "registered_count"},
                ExpressionAttributeValues={":one": 1, ":zero": 0},
            )
        except Exception:  # pragma: no cover - best effort compensation
            LOGGER.exception("failed to release reserved seat for event %s", event_id)

    def create_registration(
        self, event_id: str, attendee_name: str, attendee_email: str
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if self._find_registration_by_email(event_id, attendee_email):
            raise DuplicateRegistrationError(
                f"'{attendee_email}' is already registered for event '{event_id}'"
            )
        try:
            response = self.events_table.update_item(
                Key={"event_id": event_id},
                UpdateExpression="SET #rc = #rc + :one",
                ConditionExpression="attribute_exists(event_id) AND #rc < #cap",
                ExpressionAttributeNames={"#rc": "registered_count", "#cap": "capacity"},
                ExpressionAttributeValues={":one": 1},
                ReturnValues="ALL_NEW",
            )
        except self._condition_failed as exc:
            if self.get_event(event_id) is None:
                raise EventNotFoundError(f"event '{event_id}' not found") from exc
            raise EventFullError(f"event '{event_id}' is at full capacity") from exc

        event = _normalize_item(response.get("Attributes", {}))
        registration = {
            "event_id": event_id,
            "registration_id": str(uuid.uuid4()),
            "attendee_name": attendee_name,
            "attendee_email": attendee_email,
            "status": "confirmed",
            "created_at": _now(),
        }
        try:
            self.registrations_table.put_item(Item=registration)
        except Exception as exc:
            self._release_seat(event_id)
            raise StorageError("failed to persist registration") from exc
        return registration, event

    def health(self) -> bool:
        try:
            self._dynamodb.meta.client.describe_table(TableName=self._events_name)
            return True
        except Exception:
            LOGGER.warning("DynamoDB table %s is not reachable", self._events_name)
            return False


class InMemoryRepository:
    """In-process repository used for local development and tests."""

    def __init__(self) -> None:
        self._events: Dict[str, Dict[str, Any]] = {}
        self._registrations: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def create_event(self, title: str, date: str, capacity: int) -> Dict[str, Any]:
        item = {
            "event_id": str(uuid.uuid4()),
            "title": title,
            "date": date,
            "capacity": int(capacity),
            "registered_count": 0,
            "created_at": _now(),
        }
        with self._lock:
            self._events[item["event_id"]] = item
            self._registrations.setdefault(item["event_id"], [])
        return dict(item)

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._events.get(event_id)
            return dict(item) if item else None

    def list_events(
        self, limit: int = 100, cursor: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        offset = _cursor_offset(cursor)
        with self._lock:
            events = sorted(
                self._events.values(),
                key=lambda event: (event.get("created_at", ""), event.get("event_id", "")),
            )
        page = [dict(event) for event in events[offset:offset + limit]]
        next_cursor = None
        if offset + limit < len(events):
            next_cursor = _encode_cursor({"offset": offset + limit})
        return page, next_cursor

    def list_registrations(
        self, event_id: str, limit: int = 100, cursor: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        offset = _cursor_offset(cursor)
        with self._lock:
            rows = list(self._registrations.get(event_id, []))
        page = [dict(row) for row in rows[offset:offset + limit]]
        next_cursor = None
        if offset + limit < len(rows):
            next_cursor = _encode_cursor({"offset": offset + limit})
        return page, next_cursor

    def create_registration(
        self, event_id: str, attendee_name: str, attendee_email: str
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        with self._lock:
            event = self._events.get(event_id)
            if event is None:
                raise EventNotFoundError(f"event '{event_id}' not found")
            rows = self._registrations.setdefault(event_id, [])
            target = attendee_email.strip().lower()
            for row in rows:
                if str(row.get("attendee_email", "")).strip().lower() == target:
                    raise DuplicateRegistrationError(
                        f"'{attendee_email}' is already registered for event '{event_id}'"
                    )
            if int(event["registered_count"]) >= int(event["capacity"]):
                raise EventFullError(f"event '{event_id}' is at full capacity")
            event["registered_count"] = int(event["registered_count"]) + 1
            registration = {
                "event_id": event_id,
                "registration_id": str(uuid.uuid4()),
                "attendee_name": attendee_name,
                "attendee_email": attendee_email,
                "status": "confirmed",
                "created_at": _now(),
            }
            rows.append(registration)
            return dict(registration), dict(event)

    def health(self) -> bool:
        return True


class SqsPublisher:
    """Publishes registration messages to an SQS queue."""

    def __init__(self, client=None, queue_name: Optional[str] = None,
                 queue_url: Optional[str] = None) -> None:
        self._client = client
        self._queue_name = queue_name or os.environ.get("REGISTRATION_QUEUE", DEFAULT_QUEUE_NAME)
        self._queue_url = queue_url or os.environ.get("REGISTRATION_QUEUE_URL") or None

    @property
    def client(self):
        if self._client is None:
            self._client = sqs_client()
        return self._client

    def queue_url(self) -> str:
        if self._queue_url is None:
            response = self.client.get_queue_url(QueueName=self._queue_name)
            self._queue_url = response["QueueUrl"]
        return self._queue_url

    def publish(self, message: Dict[str, Any]) -> bool:
        try:
            self.client.send_message(
                QueueUrl=self.queue_url(),
                MessageBody=json.dumps(message, default=str),
            )
            return True
        except Exception:
            LOGGER.exception("failed to publish registration message to %s", self._queue_name)
            return False

    def health(self) -> bool:
        try:
            self.queue_url()
            return True
        except Exception:
            LOGGER.warning("SQS queue %s is not reachable", self._queue_name)
            return False


class InMemoryPublisher:
    """Collects published messages in memory (local development and tests)."""

    def __init__(self, fail: bool = False) -> None:
        self.messages: List[Dict[str, Any]] = []
        self.fail = fail

    def publish(self, message: Dict[str, Any]) -> bool:
        if self.fail:
            LOGGER.warning("in-memory publisher configured to fail")
            return False
        self.messages.append(dict(message))
        return True

    def health(self) -> bool:
        return not self.fail
