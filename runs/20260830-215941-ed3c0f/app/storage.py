"""Data access layer for the personal notes API.

All DynamoDB access is funnelled through :class:`DynamoNotesRepository` so the
HTTP layer can be exercised with an in-memory fake in tests.  The boto3 client
honours ``AWS_ENDPOINT_URL`` (LocalStack) and defaults to ``us-east-1``.
"""

import base64
import binascii
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3

DEFAULT_TABLE_NAME = "notes"
DEFAULT_REGION = "us-east-1"


class NoteNotFound(Exception):
    """Raised when an operation targets a note id that does not exist."""


class InvalidTokenError(Exception):
    """Raised when a pagination cursor cannot be decoded."""


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string ending in ``Z``."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def table_name() -> str:
    """Resolve the DynamoDB table name from the environment."""
    return os.environ.get("NOTES_TABLE_NAME", os.environ.get("NOTES_TABLE", DEFAULT_TABLE_NAME))


def dynamodb_resource():
    """Build a DynamoDB resource pointed at LocalStack when configured."""
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def encode_token(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB ``LastEvaluatedKey`` as an opaque cursor string."""
    if not key:
        return None
    raw = json.dumps(key, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: str) -> Dict[str, Any]:
    """Decode an opaque cursor string back into a DynamoDB key."""
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        key = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidTokenError("Invalid next_token") from exc
    if not isinstance(key, dict) or not key:
        raise InvalidTokenError("Invalid next_token")
    return key


def normalize_note(item: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a raw DynamoDB item into the API note shape."""
    return {
        "id": str(item.get("id", "")),
        "title": str(item.get("title", "")),
        "body": str(item.get("body", "")),
        "created_at": str(item.get("created_at", "")),
        "updated_at": str(item.get("updated_at", "")),
    }


class DynamoNotesRepository:
    """DynamoDB backed repository for note items."""

    def __init__(self, name: Optional[str] = None) -> None:
        self.table_name = name or table_name()
        self._table = None

    @property
    def table(self):
        """Lazily created DynamoDB Table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    def _condition_failed(self):
        """Return the ConditionalCheckFailedException class for this client."""
        return self.table.meta.client.exceptions.ConditionalCheckFailedException

    def health(self) -> Tuple[bool, str]:
        """Check whether the notes table can be described."""
        try:
            response = self.table.meta.client.describe_table(TableName=self.table_name)
            return True, str(response.get("Table", {}).get("TableStatus", "UNKNOWN"))
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return False, str(exc)

    def create(self, note: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a new note item and return it."""
        item = normalize_note(note)
        self.table.put_item(Item=item)
        return item

    def get(self, note_id: str) -> Optional[Dict[str, Any]]:
        """Return a note by id or ``None`` when it does not exist."""
        response = self.table.get_item(Key={"id": note_id})
        item = response.get("Item")
        if not item:
            return None
        return normalize_note(item)

    def list_notes(
        self,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Scan the table returning one page of notes plus a cursor."""
        kwargs: Dict[str, Any] = {"Limit": int(limit)}
        if next_token:
            kwargs["ExclusiveStartKey"] = decode_token(next_token)
        response = self.table.scan(**kwargs)
        items = [normalize_note(i) for i in response.get("Items", [])]
        return items, encode_token(response.get("LastEvaluatedKey"))

    def update(self, note_id: str, fields: Dict[str, Any], updated_at: str) -> Dict[str, Any]:
        """Apply a partial update to an existing note and return the result."""
        payload = dict(fields)
        payload["updated_at"] = updated_at
        names: Dict[str, str] = {"#pk": "id"}
        values: Dict[str, Any] = {}
        assignments = []
        for index, key in enumerate(sorted(payload)):
            names["#n%d" % index] = key
            values[":v%d" % index] = payload[key]
            assignments.append("#n%d = :v%d" % (index, index))
        try:
            response = self.table.update_item(
                Key={"id": note_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(#pk)",
                ReturnValues="ALL_NEW",
            )
        except self._condition_failed() as exc:
            raise NoteNotFound(note_id) from exc
        return normalize_note(response.get("Attributes", {}))

    def delete(self, note_id: str) -> None:
        """Delete a note, raising :class:`NoteNotFound` when absent."""
        try:
            self.table.delete_item(
                Key={"id": note_id},
                ExpressionAttributeNames={"#pk": "id"},
                ConditionExpression="attribute_exists(#pk)",
            )
        except self._condition_failed() as exc:
            raise NoteNotFound(note_id) from exc
