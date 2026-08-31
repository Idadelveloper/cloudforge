"""Data access layer for the personal notes API.

The HTTP layer only ever talks to :class:`NotesRepository`.  Two implementations
are provided: :class:`DynamoDBNotesRepository` (boto3, LocalStack friendly) and
:class:`InMemoryNotesRepository` (used by tests and local experimentation).
"""

import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOGGER = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE_NAME = "notes"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
PARTITION_KEY = "note_id"

CONDITIONAL_FAILURE_CODES = (
    "ConditionalCheckFailedException",
    "ConditionalCheckFailed",
)


class StorageError(RuntimeError):
    """Raised when the underlying datastore fails unexpectedly."""


class NoteNotFoundError(LookupError):
    """Raised when the requested note does not exist."""

    def __init__(self, note_id: str) -> None:
        super().__init__("note '{0}' was not found".format(note_id))
        self.note_id = note_id


class InvalidCursorError(ValueError):
    """Raised when a pagination cursor cannot be decoded."""


def aws_region() -> str:
    """Return the configured AWS region."""
    return os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)


def aws_endpoint_url() -> Optional[str]:
    """Return the AWS endpoint override (LocalStack) if configured."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def notes_table_name() -> str:
    """Return the DynamoDB table name holding the notes."""
    return os.environ.get("NOTES_TABLE_NAME", DEFAULT_TABLE_NAME)


def dynamodb_resource() -> Any:
    """Create a DynamoDB resource honouring AWS_ENDPOINT_URL."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def encode_cursor(last_key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey into an opaque cursor."""
    if not last_key:
        return None
    raw = json.dumps(last_key, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode an opaque cursor back into a DynamoDB ExclusiveStartKey."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise InvalidCursorError("cursor is not a valid pagination token") from exc
    if not isinstance(decoded, dict) or PARTITION_KEY not in decoded:
        raise InvalidCursorError("cursor is not a valid pagination token")
    return decoded


def _is_conditional_failure(exc: ClientError) -> bool:
    """Return True when the ClientError is a failed condition expression."""
    code = exc.response.get("Error", {}).get("Code", "") if exc.response else ""
    return code in CONDITIONAL_FAILURE_CODES


class NotesRepository:
    """Interface implemented by the notes storage backends."""

    table_name: str = DEFAULT_TABLE_NAME

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a brand new note item and return it."""
        raise NotImplementedError

    def get(self, note_id: str) -> Dict[str, Any]:
        """Return a note or raise :class:`NoteNotFoundError`."""
        raise NotImplementedError

    def list(
        self,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of notes plus the next cursor."""
        raise NotImplementedError

    def update(self, note_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a partial update and return the stored note."""
        raise NotImplementedError

    def delete(self, note_id: str) -> None:
        """Delete a note or raise :class:`NoteNotFoundError`."""
        raise NotImplementedError

    def healthy(self) -> bool:
        """Return True when the datastore is reachable."""
        raise NotImplementedError


class DynamoDBNotesRepository(NotesRepository):
    """DynamoDB backed notes repository."""

    def __init__(self, table: Any = None, table_name: Optional[str] = None) -> None:
        self._table = table
        self.table_name = table_name or notes_table_name()

    def _table_handle(self) -> Any:
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(item)
        try:
            self._table_handle().put_item(Item=payload)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to store note: {0}".format(exc)) from exc
        return payload

    def get(self, note_id: str) -> Dict[str, Any]:
        try:
            response = self._table_handle().get_item(Key={PARTITION_KEY: note_id})
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to read note: {0}".format(exc)) from exc
        item = (response or {}).get("Item")
        if not item:
            raise NoteNotFoundError(note_id)
        return dict(item)

    def list(
        self,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        page_size = max(1, min(int(limit), MAX_PAGE_SIZE))
        kwargs: Dict[str, Any] = {"Limit": page_size}
        start_key = decode_cursor(cursor)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        try:
            response = self._table_handle().scan(**kwargs)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to list notes: {0}".format(exc)) from exc
        items = [dict(entry) for entry in (response or {}).get("Items", [])]
        return items, encode_cursor((response or {}).get("LastEvaluatedKey"))

    def update(self, note_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        if not updates:
            raise ValueError("no updates supplied")
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments: List[str] = []
        for index, field in enumerate(sorted(updates)):
            name_placeholder = "#f{0}".format(index)
            value_placeholder = ":v{0}".format(index)
            names[name_placeholder] = field
            values[value_placeholder] = updates[field]
            assignments.append("{0} = {1}".format(name_placeholder, value_placeholder))
        try:
            response = self._table_handle().update_item(
                Key={PARTITION_KEY: note_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists({0})".format(PARTITION_KEY),
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise NoteNotFoundError(note_id) from exc
            raise StorageError("failed to update note: {0}".format(exc)) from exc
        except BotoCoreError as exc:
            raise StorageError("failed to update note: {0}".format(exc)) from exc
        return dict((response or {}).get("Attributes") or {})

    def delete(self, note_id: str) -> None:
        try:
            self._table_handle().delete_item(
                Key={PARTITION_KEY: note_id},
                ConditionExpression="attribute_exists({0})".format(PARTITION_KEY),
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise NoteNotFoundError(note_id) from exc
            raise StorageError("failed to delete note: {0}".format(exc)) from exc
        except BotoCoreError as exc:
            raise StorageError("failed to delete note: {0}".format(exc)) from exc

    def healthy(self) -> bool:
        try:
            table = self._table_handle()
            client = table.meta.client
            client.describe_table(TableName=self.table_name)
            return True
        except (ClientError, BotoCoreError, AttributeError) as exc:
            LOGGER.warning("notes table %s is not reachable: %s", self.table_name, exc)
            return False


class InMemoryNotesRepository(NotesRepository):
    """Dictionary backed repository used for tests and offline runs."""

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None) -> None:
        self.table_name = notes_table_name()
        self._items: Dict[str, Dict[str, Any]] = {}
        for entry in items or []:
            self._items[str(entry[PARTITION_KEY])] = dict(entry)
        self.healthy_flag = True

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(item)
        self._items[str(payload[PARTITION_KEY])] = payload
        return dict(payload)

    def get(self, note_id: str) -> Dict[str, Any]:
        item = self._items.get(note_id)
        if item is None:
            raise NoteNotFoundError(note_id)
        return dict(item)

    def list(
        self,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        page_size = max(1, min(int(limit), MAX_PAGE_SIZE))
        start_key = decode_cursor(cursor)
        keys = sorted(self._items)
        if start_key:
            marker = str(start_key.get(PARTITION_KEY))
            keys = [key for key in keys if key > marker]
        page = keys[:page_size]
        next_cursor = None
        if page and len(keys) > page_size:
            next_cursor = encode_cursor({PARTITION_KEY: page[-1]})
        return [dict(self._items[key]) for key in page], next_cursor

    def update(self, note_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        if not updates:
            raise ValueError("no updates supplied")
        item = self._items.get(note_id)
        if item is None:
            raise NoteNotFoundError(note_id)
        item.update(updates)
        return dict(item)

    def delete(self, note_id: str) -> None:
        if self._items.pop(note_id, None) is None:
            raise NoteNotFoundError(note_id)

    def healthy(self) -> bool:
        return self.healthy_flag
