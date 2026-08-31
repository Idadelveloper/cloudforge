"""Data access layer for the personal notes API.

The repository interface hides DynamoDB behind a tiny abstraction so the HTTP
layer stays testable.  ``DynamoDBNotesRepository`` talks to a real (or
LocalStack) table; ``InMemoryNotesRepository`` is used by tests and for local
experiments without AWS.
"""
import base64
import binascii
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)

DEFAULT_TABLE_NAME = "notes-table"
DEFAULT_REGION = "us-east-1"


class StorageError(RuntimeError):
    """Raised when the underlying storage backend fails unexpectedly."""


class InvalidTokenError(ValueError):
    """Raised when a pagination token cannot be decoded."""


def table_name() -> str:
    """Name of the DynamoDB notes table."""
    return os.environ.get("NOTES_TABLE_NAME", DEFAULT_TABLE_NAME)


def aws_region() -> str:
    """Region used for every AWS client."""
    return os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)


def dynamodb_resource():
    """Create a DynamoDB resource honouring AWS_ENDPOINT_URL (LocalStack)."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def encode_token(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a pagination cursor into an opaque URL-safe string."""
    if not payload:
        return None
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode an opaque pagination cursor produced by :func:`encode_token`."""
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidTokenError("next_token is not a valid cursor") from exc
    if not isinstance(payload, dict):
        raise InvalidTokenError("next_token is not a valid cursor")
    return payload


class NotesRepository:
    """Abstract notes repository."""

    def put_note(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a brand new note item."""
        raise NotImplementedError

    def get_note(self, owner_id: str, note_id: str) -> Optional[Dict[str, Any]]:
        """Return a note or ``None`` when it does not exist."""
        raise NotImplementedError

    def list_notes(
        self,
        owner_id: str,
        limit: int,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of notes and the cursor for the next page."""
        raise NotImplementedError

    def update_note(
        self,
        owner_id: str,
        note_id: str,
        title: str,
        body: str,
        updated_at: str,
    ) -> Optional[Dict[str, Any]]:
        """Replace title/body of an existing note; ``None`` when missing."""
        raise NotImplementedError

    def delete_note(self, owner_id: str, note_id: str) -> bool:
        """Delete a note, returning ``False`` when it does not exist."""
        raise NotImplementedError


class DynamoDBNotesRepository(NotesRepository):
    """DynamoDB backed repository (partition key owner_id, sort key note_id)."""

    def __init__(self, name: Optional[str] = None) -> None:
        self._name = name or table_name()
        self._table = None

    def table(self):
        """Lazily resolve the boto3 Table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(self._name)
        return self._table

    def put_note(self, item: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self.table().put_item(Item=dict(item))
        except ClientError as exc:
            raise StorageError("put_item failed: {0}".format(exc)) from exc
        return dict(item)

    def get_note(self, owner_id: str, note_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.table().get_item(Key={"owner_id": owner_id, "note_id": note_id})
        except ClientError as exc:
            raise StorageError("get_item failed: {0}".format(exc)) from exc
        item = response.get("Item")
        return dict(item) if item else None

    def list_notes(
        self,
        owner_id: str,
        limit: int,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "Limit": limit,
            "ScanIndexForward": False,
        }
        start_key = decode_token(next_token)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        try:
            response = self.table().query(**kwargs)
        except ClientError as exc:
            raise StorageError("query failed: {0}".format(exc)) from exc
        items = [dict(item) for item in response.get("Items", [])]
        return items, encode_token(response.get("LastEvaluatedKey"))

    def update_note(
        self,
        owner_id: str,
        note_id: str,
        title: str,
        body: str,
        updated_at: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            response = self.table().update_item(
                Key={"owner_id": owner_id, "note_id": note_id},
                UpdateExpression="SET #title = :title, #body = :body, #updated_at = :updated_at",
                ExpressionAttributeNames={
                    "#title": "title",
                    "#body": "body",
                    "#updated_at": "updated_at",
                },
                ExpressionAttributeValues={
                    ":title": title,
                    ":body": body,
                    ":updated_at": updated_at,
                },
                ConditionExpression="attribute_exists(note_id)",
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return None
            raise StorageError("update_item failed: {0}".format(exc)) from exc
        attributes = response.get("Attributes") or {}
        return dict(attributes)

    def delete_note(self, owner_id: str, note_id: str) -> bool:
        try:
            self.table().delete_item(
                Key={"owner_id": owner_id, "note_id": note_id},
                ConditionExpression="attribute_exists(note_id)",
            )
        except ClientError as exc:
            if self._is_conditional_failure(exc):
                return False
            raise StorageError("delete_item failed: {0}".format(exc)) from exc
        return True

    @staticmethod
    def _is_conditional_failure(exc: ClientError) -> bool:
        error = getattr(exc, "response", {}) or {}
        code = (error.get("Error") or {}).get("Code")
        return code == "ConditionalCheckFailedException"


class InMemoryNotesRepository(NotesRepository):
    """Thread-safe in-memory repository used for tests and local runs."""

    def __init__(self) -> None:
        self._rows: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def put_note(self, item: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            owner = self._rows.setdefault(item["owner_id"], {})
            owner[item["note_id"]] = dict(item)
        return dict(item)

    def get_note(self, owner_id: str, note_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._rows.get(owner_id, {}).get(note_id)
            return dict(item) if item else None

    def list_notes(
        self,
        owner_id: str,
        limit: int,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        payload = decode_token(next_token) or {}
        try:
            offset = int(payload.get("offset", 0))
        except (TypeError, ValueError) as exc:
            raise InvalidTokenError("next_token is not a valid cursor") from exc
        if offset < 0:
            raise InvalidTokenError("next_token is not a valid cursor")
        with self._lock:
            notes = sorted(
                (dict(item) for item in self._rows.get(owner_id, {}).values()),
                key=lambda note: note["note_id"],
                reverse=True,
            )
        page = notes[offset:offset + limit]
        new_offset = offset + len(page)
        token = encode_token({"offset": new_offset}) if new_offset < len(notes) else None
        return page, token

    def update_note(
        self,
        owner_id: str,
        note_id: str,
        title: str,
        body: str,
        updated_at: str,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._rows.get(owner_id, {}).get(note_id)
            if item is None:
                return None
            item["title"] = title
            item["body"] = body
            item["updated_at"] = updated_at
            return dict(item)

    def delete_note(self, owner_id: str, note_id: str) -> bool:
        with self._lock:
            owner = self._rows.get(owner_id, {})
            if note_id not in owner:
                return False
            del owner[note_id]
            return True


def build_repository() -> NotesRepository:
    """Build the repository selected by the NOTES_BACKEND env variable."""
    backend = os.environ.get("NOTES_BACKEND", "dynamodb").strip().lower()
    if backend == "memory":
        LOGGER.info("using in-memory notes repository")
        return InMemoryNotesRepository()
    LOGGER.info("using dynamodb notes repository table=%s", table_name())
    return DynamoDBNotesRepository()
