"""Data access layer for the personal notes API.

Exposes an abstract ``NotesRepository`` plus two implementations:

* ``DynamoDBNotesRepository`` - production implementation backed by boto3.
* ``InMemoryNotesRepository`` - dependency-free fake used by tests/local runs.
"""
import abc
import base64
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

DEFAULT_TABLE_NAME = "notes-table"
UPDATABLE_FIELDS = ("title", "body", "updated_at")
CONDITION_FAILED = "ConditionalCheckFailedException"


class InvalidCursorError(ValueError):
    """Raised when a client supplies a malformed pagination cursor."""


def table_name() -> str:
    """Return the configured DynamoDB table name."""
    return os.environ.get("NOTES_TABLE_NAME", DEFAULT_TABLE_NAME)


def dynamodb_resource():
    """Build a DynamoDB resource honouring AWS_ENDPOINT_URL (LocalStack)."""
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def encode_cursor(key: Dict[str, Any]) -> str:
    """Encode a DynamoDB LastEvaluatedKey into an opaque cursor string."""
    raw = json.dumps(key, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> Dict[str, Any]:
    """Decode an opaque cursor back into a DynamoDB exclusive start key."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        key = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise InvalidCursorError("cursor is not a valid pagination token") from exc
    if not isinstance(key, dict) or not key:
        raise InvalidCursorError("cursor is not a valid pagination token")
    return key


def _filter_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in changes.items() if k in UPDATABLE_FIELDS}


class NotesRepository(abc.ABC):
    """Persistence contract used by the HTTP layer."""

    @abc.abstractmethod
    def create(self, note: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a brand new note and return it."""

    @abc.abstractmethod
    def get(self, user_id: str, note_id: str) -> Optional[Dict[str, Any]]:
        """Return a single note or None when missing."""

    @abc.abstractmethod
    def list(
        self,
        user_id: str,
        limit: int = 25,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of notes plus the next cursor (or None)."""

    @abc.abstractmethod
    def update(self, user_id: str, note_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply a partial update; return the new note or None when missing."""

    @abc.abstractmethod
    def delete(self, user_id: str, note_id: str) -> bool:
        """Delete a note; return True when a note was removed."""


class DynamoDBNotesRepository(NotesRepository):
    """DynamoDB implementation of :class:`NotesRepository`."""

    def __init__(self, table: Any = None, name: Optional[str] = None) -> None:
        self._table = table
        self._name = name or table_name()

    @property
    def name(self) -> str:
        return self._name

    @property
    def table(self) -> Any:
        if self._table is None:
            self._table = dynamodb_resource().Table(self._name)
        return self._table

    @staticmethod
    def _key(user_id: str, note_id: str) -> Dict[str, str]:
        return {"user_id": user_id, "note_id": note_id}

    def create(self, note: Dict[str, Any]) -> Dict[str, Any]:
        self.table.put_item(Item=dict(note))
        return dict(note)

    def get(self, user_id: str, note_id: str) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key=self._key(user_id, note_id))
        item = response.get("Item")
        return dict(item) if item else None

    def list(
        self,
        user_id: str,
        limit: int = 25,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        params: Dict[str, Any] = {
            "KeyConditionExpression": Key("user_id").eq(user_id),
            "Limit": limit,
            "ScanIndexForward": True,
        }
        if cursor:
            params["ExclusiveStartKey"] = decode_cursor(cursor)
        response = self.table.query(**params)
        items = [dict(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        next_cursor = encode_cursor(last_key) if last_key else None
        return items, next_cursor

    def update(self, user_id: str, note_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        payload = _filter_changes(changes)
        if not payload:
            return self.get(user_id, note_id)
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments: List[str] = []
        for index, (field, value) in enumerate(sorted(payload.items())):
            names["#k{0}".format(index)] = field
            values[":v{0}".format(index)] = value
            assignments.append("#k{0} = :v{0}".format(index))
        try:
            response = self.table.update_item(
                Key=self._key(user_id, note_id),
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(note_id)",
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == CONDITION_FAILED:
                return None
            raise
        attributes = response.get("Attributes")
        return dict(attributes) if attributes else None

    def delete(self, user_id: str, note_id: str) -> bool:
        try:
            self.table.delete_item(
                Key=self._key(user_id, note_id),
                ConditionExpression="attribute_exists(note_id)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == CONDITION_FAILED:
                return False
            raise
        return True


class InMemoryNotesRepository(NotesRepository):
    """In-memory repository used for tests and offline local development."""

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def create(self, note: Dict[str, Any]) -> Dict[str, Any]:
        bucket = self._data.setdefault(note["user_id"], {})
        bucket[note["note_id"]] = dict(note)
        return dict(note)

    def get(self, user_id: str, note_id: str) -> Optional[Dict[str, Any]]:
        note = self._data.get(user_id, {}).get(note_id)
        return dict(note) if note else None

    def list(
        self,
        user_id: str,
        limit: int = 25,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        notes = [dict(n) for n in self._data.get(user_id, {}).values()]
        notes.sort(key=lambda item: item["note_id"])
        if cursor:
            key = decode_cursor(cursor)
            start_after = key.get("note_id")
            if not isinstance(start_after, str):
                raise InvalidCursorError("cursor is not a valid pagination token")
            notes = [n for n in notes if n["note_id"] > start_after]
        page = notes[:limit]
        next_cursor = None
        if len(notes) > limit and page:
            next_cursor = encode_cursor({"user_id": user_id, "note_id": page[-1]["note_id"]})
        return page, next_cursor

    def update(self, user_id: str, note_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        note = self._data.get(user_id, {}).get(note_id)
        if note is None:
            return None
        note.update(_filter_changes(changes))
        return dict(note)

    def delete(self, user_id: str, note_id: str) -> bool:
        bucket = self._data.get(user_id, {})
        if note_id not in bucket:
            return False
        del bucket[note_id]
        return True
