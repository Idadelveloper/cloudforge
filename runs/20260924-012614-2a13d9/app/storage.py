"""Data access layer for the personal notes API.

A small repository interface hides DynamoDB behind plain dictionaries so the
web layer never imports boto3 directly. ``InMemoryNotesRepository`` provides an
equivalent implementation used by the offline test-suite.
"""

import base64
import binascii
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE_NAME = "notes"
PARTITION_KEY = "note_id"


class NoteNotFoundError(Exception):
    """Raised when a note id does not exist in the backing store."""


class InvalidCursorError(Exception):
    """Raised when a pagination cursor cannot be decoded."""


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def table_name() -> str:
    """Return the configured DynamoDB table name."""
    return os.environ.get("NOTES_TABLE_NAME", DEFAULT_TABLE_NAME)


def region_name() -> str:
    """Return the configured AWS region."""
    return os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)


def dynamodb_resource() -> Any:
    """Build a DynamoDB resource honouring AWS_ENDPOINT_URL (LocalStack)."""
    return boto3.resource(
        "dynamodb",
        region_name=region_name(),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def encode_cursor(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey into an opaque cursor string."""
    if not key:
        return None
    raw = json.dumps(key, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode an opaque cursor back into a DynamoDB exclusive start key."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursorError("invalid pagination cursor") from exc
    if not isinstance(decoded, dict):
        raise InvalidCursorError("invalid pagination cursor")
    return decoded


def normalise_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a raw DynamoDB item into the API note shape."""
    return {
        "note_id": str(item.get("note_id", "")),
        "title": str(item.get("title", "")),
        "body": str(item.get("body", "") or ""),
        "created_at": str(item.get("created_at", "")),
        "updated_at": str(item.get("updated_at", "")),
    }


class NotesRepository:
    """Interface implemented by every notes storage backend."""

    def create(self, title: str, body: str = "") -> Dict[str, Any]:
        raise NotImplementedError

    def list_notes(
        self, limit: int = 50, cursor: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        raise NotImplementedError

    def get(self, note_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def update(
        self, note_id: str, title: Optional[str] = None, body: Optional[str] = None
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def delete(self, note_id: str) -> None:
        raise NotImplementedError

    def healthy(self) -> bool:
        raise NotImplementedError


class DynamoDBNotesRepository(NotesRepository):
    """Repository backed by a single DynamoDB table."""

    def __init__(self, table: Any = None, name: Optional[str] = None) -> None:
        self._table = table
        self._name = name or table_name()

    @property
    def name(self) -> str:
        """Name of the backing DynamoDB table."""
        return self._name

    @property
    def table(self) -> Any:
        """Lazily resolve the boto3 Table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(self._name)
        return self._table

    def create(self, title: str, body: str = "") -> Dict[str, Any]:
        now = utc_now_iso()
        item = {
            "note_id": str(uuid.uuid4()),
            "title": title,
            "body": body or "",
            "created_at": now,
            "updated_at": now,
        }
        self.table.put_item(Item=item)
        return normalise_item(item)

    def list_notes(
        self, limit: int = 50, cursor: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": limit}
        start_key = decode_cursor(cursor)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = self.table.scan(**kwargs)
        items = [normalise_item(raw) for raw in response.get("Items", [])]
        return items, encode_cursor(response.get("LastEvaluatedKey"))

    def get(self, note_id: str) -> Dict[str, Any]:
        response = self.table.get_item(Key={PARTITION_KEY: note_id})
        item = response.get("Item")
        if not item:
            raise NoteNotFoundError(note_id)
        return normalise_item(item)

    def update(
        self, note_id: str, title: Optional[str] = None, body: Optional[str] = None
    ) -> Dict[str, Any]:
        names = {"#updated_at": "updated_at"}
        values: Dict[str, Any] = {":updated_at": utc_now_iso()}
        assignments = ["#updated_at = :updated_at"]
        if title is not None:
            names["#title"] = "title"
            values[":title"] = title
            assignments.append("#title = :title")
        if body is not None:
            names["#body"] = "body"
            values[":body"] = body
            assignments.append("#body = :body")
        try:
            response = self.table.update_item(
                Key={PARTITION_KEY: note_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(note_id)",
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                raise NoteNotFoundError(note_id) from exc
            raise
        return normalise_item(response.get("Attributes", {}))

    def delete(self, note_id: str) -> None:
        try:
            self.table.delete_item(
                Key={PARTITION_KEY: note_id},
                ConditionExpression="attribute_exists(note_id)",
            )
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                raise NoteNotFoundError(note_id) from exc
            raise

    def healthy(self) -> bool:
        try:
            self.table.meta.client.describe_table(TableName=self._name)
            return True
        except Exception:  # noqa: BLE001 - health probe must never raise
            return False

    @staticmethod
    def _is_conditional_failure(exc: ClientError) -> bool:
        error = getattr(exc, "response", {}) or {}
        code = error.get("Error", {}).get("Code")
        return code == "ConditionalCheckFailedException"


class InMemoryNotesRepository(NotesRepository):
    """Dictionary backed repository used for offline tests and local runs."""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        self.available = True

    def create(self, title: str, body: str = "") -> Dict[str, Any]:
        now = utc_now_iso()
        item = {
            "note_id": str(uuid.uuid4()),
            "title": title,
            "body": body or "",
            "created_at": now,
            "updated_at": now,
        }
        self._items[item["note_id"]] = item
        return normalise_item(item)

    def list_notes(
        self, limit: int = 50, cursor: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        ordered = list(self._items.values())
        start = 0
        start_key = decode_cursor(cursor)
        if start_key:
            last_id = start_key.get(PARTITION_KEY)
            ids = [entry["note_id"] for entry in ordered]
            if last_id in ids:
                start = ids.index(last_id) + 1
        page = ordered[start:start + limit]
        next_cursor = None
        if page and (start + limit) < len(ordered):
            next_cursor = encode_cursor({PARTITION_KEY: page[-1]["note_id"]})
        return [normalise_item(entry) for entry in page], next_cursor

    def get(self, note_id: str) -> Dict[str, Any]:
        item = self._items.get(note_id)
        if not item:
            raise NoteNotFoundError(note_id)
        return normalise_item(item)

    def update(
        self, note_id: str, title: Optional[str] = None, body: Optional[str] = None
    ) -> Dict[str, Any]:
        item = self._items.get(note_id)
        if not item:
            raise NoteNotFoundError(note_id)
        if title is not None:
            item["title"] = title
        if body is not None:
            item["body"] = body
        item["updated_at"] = utc_now_iso()
        return normalise_item(item)

    def delete(self, note_id: str) -> None:
        if note_id not in self._items:
            raise NoteNotFoundError(note_id)
        del self._items[note_id]

    def healthy(self) -> bool:
        return self.available
