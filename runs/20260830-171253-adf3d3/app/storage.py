"""Data access layer for the personal notes API.

The DynamoDB implementation is hidden behind :class:`NotesRepository` so the
application layer can be tested offline with :class:`InMemoryNotesRepository`.
"""

import base64
import json
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key

DEFAULT_TABLE_NAME = "notes-table"
DEFAULT_REGION = "us-east-1"
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
CONDITIONAL_FAILURE = "ConditionalCheckFailedException"


class NoteNotFoundError(Exception):
    """Raised when a note does not exist for the requesting user."""


class InvalidPageTokenError(Exception):
    """Raised when a supplied pagination token cannot be decoded."""


def table_name() -> str:
    """Return the configured DynamoDB table name."""
    return os.environ.get("NOTES_TABLE_NAME", DEFAULT_TABLE_NAME)


def dynamodb_resource():
    """Create a DynamoDB resource, honouring AWS_ENDPOINT_URL for LocalStack."""
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def encode_token(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey into an opaque pagination token."""
    if not key:
        return None
    raw = json.dumps(key, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def decode_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode an opaque pagination token back into a DynamoDB key."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8"))
        key = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidPageTokenError("next_token is not a valid pagination token") from exc
    if not isinstance(key, dict):
        raise InvalidPageTokenError("next_token is not a valid pagination token")
    return key


class NotesRepository(ABC):
    """Interface for note persistence."""

    @abstractmethod
    def create_note(self, note: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a fully-formed note and return it."""

    @abstractmethod
    def get_note(self, user_id: str, note_id: str) -> Dict[str, Any]:
        """Return a note or raise :class:`NoteNotFoundError`."""

    @abstractmethod
    def list_notes(
        self,
        user_id: str,
        limit: int = DEFAULT_PAGE_SIZE,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of notes and the token for the next page."""

    @abstractmethod
    def update_note(self, user_id: str, note_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a partial update and return the stored note."""

    @abstractmethod
    def delete_note(self, user_id: str, note_id: str) -> None:
        """Delete a note or raise :class:`NoteNotFoundError`."""


class DynamoDBNotesRepository(NotesRepository):
    """DynamoDB-backed repository for notes."""

    def __init__(self, name: Optional[str] = None, resource: Any = None) -> None:
        self._table_name = name or table_name()
        self._resource = resource
        self._table = None

    @property
    def table(self):
        """Lazily resolve the DynamoDB Table object."""
        if self._table is None:
            resource = self._resource if self._resource is not None else dynamodb_resource()
            self._table = resource.Table(self._table_name)
        return self._table

    @staticmethod
    def _is_conditional_failure(exc: Exception) -> bool:
        """Return True when the exception is a DynamoDB conditional check failure."""
        if type(exc).__name__ == CONDITIONAL_FAILURE:
            return True
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            error = response.get("Error", {})
            if isinstance(error, dict) and error.get("Code") == CONDITIONAL_FAILURE:
                return True
        return False

    def create_note(self, note: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(note)
        self.table.put_item(Item=item)
        return item

    def get_note(self, user_id: str, note_id: str) -> Dict[str, Any]:
        response = self.table.get_item(Key={"user_id": user_id, "note_id": note_id})
        item = response.get("Item") if isinstance(response, dict) else None
        if not item:
            raise NoteNotFoundError(f"Note '{note_id}' was not found")
        return dict(item)

    def list_notes(
        self,
        user_id: str,
        limit: int = DEFAULT_PAGE_SIZE,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        page_size = max(1, min(int(limit), MAX_PAGE_SIZE))
        params: Dict[str, Any] = {
            "KeyConditionExpression": Key("user_id").eq(user_id),
            "Limit": page_size,
        }
        start_key = decode_token(next_token)
        if start_key:
            params["ExclusiveStartKey"] = start_key
        response = self.table.query(**params)
        items = [dict(item) for item in response.get("Items", [])]
        return items, encode_token(response.get("LastEvaluatedKey"))

    def update_note(self, user_id: str, note_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        if not changes:
            raise ValueError("changes must not be empty")
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments: List[str] = []
        for index, field in enumerate(sorted(changes)):
            names[f"#f{index}"] = field
            values[f":v{index}"] = changes[field]
            assignments.append(f"#f{index} = :v{index}")
        try:
            response = self.table.update_item(
                Key={"user_id": user_id, "note_id": note_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(note_id)",
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:  # noqa: BLE001 - narrowed via _is_conditional_failure
            if self._is_conditional_failure(exc):
                raise NoteNotFoundError(f"Note '{note_id}' was not found") from exc
            raise
        return dict(response.get("Attributes", {}))

    def delete_note(self, user_id: str, note_id: str) -> None:
        try:
            self.table.delete_item(
                Key={"user_id": user_id, "note_id": note_id},
                ConditionExpression="attribute_exists(note_id)",
            )
        except Exception as exc:  # noqa: BLE001 - narrowed via _is_conditional_failure
            if self._is_conditional_failure(exc):
                raise NoteNotFoundError(f"Note '{note_id}' was not found") from exc
            raise


class InMemoryNotesRepository(NotesRepository):
    """Dictionary-backed repository used for tests and local development."""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def create_note(self, note: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(note)
        self._items.setdefault(item["user_id"], {})[item["note_id"]] = item
        return dict(item)

    def get_note(self, user_id: str, note_id: str) -> Dict[str, Any]:
        item = self._items.get(user_id, {}).get(note_id)
        if item is None:
            raise NoteNotFoundError(f"Note '{note_id}' was not found")
        return dict(item)

    def list_notes(
        self,
        user_id: str,
        limit: int = DEFAULT_PAGE_SIZE,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        page_size = max(1, min(int(limit), MAX_PAGE_SIZE))
        start_key = decode_token(next_token)
        stored = self._items.get(user_id, {}).values()
        notes = [dict(item) for item in sorted(stored, key=lambda note: note["note_id"])]
        if start_key:
            marker = str(start_key.get("note_id", ""))
            notes = [note for note in notes if note["note_id"] > marker]
        page = notes[:page_size]
        token = None
        if page and len(notes) > page_size:
            token = encode_token({"user_id": user_id, "note_id": page[-1]["note_id"]})
        return page, token

    def update_note(self, user_id: str, note_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        item = self._items.get(user_id, {}).get(note_id)
        if item is None:
            raise NoteNotFoundError(f"Note '{note_id}' was not found")
        item.update(changes)
        return dict(item)

    def delete_note(self, user_id: str, note_id: str) -> None:
        user_items = self._items.get(user_id, {})
        if note_id not in user_items:
            raise NoteNotFoundError(f"Note '{note_id}' was not found")
        del user_items[note_id]
