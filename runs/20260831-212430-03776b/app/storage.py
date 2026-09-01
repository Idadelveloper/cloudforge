"""Data access layer for the contact-form backend.

All AWS access is hidden behind :class:`MessageRepository` so the application
can be tested without touching the network. The DynamoDB implementation honours
``AWS_ENDPOINT_URL`` (LocalStack) and defaults to region ``us-east-1``.
"""

import base64
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_TABLE_NAME = "contact-form-messages"
DEFAULT_SECRET_NAME = "contact-form/admin-api-key"  # nosec B105 - resource name, not a credential


class StorageError(RuntimeError):
    """Raised when the underlying datastore fails."""


class InvalidTokenError(ValueError):
    """Raised when a pagination cursor cannot be decoded."""


def _aws_kwargs() -> Dict[str, Any]:
    """Common boto3 client/resource keyword arguments."""
    return {
        "region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        "endpoint_url": os.environ.get("AWS_ENDPOINT_URL") or None,
    }


def dynamodb_resource() -> Any:
    """Return a DynamoDB service resource."""
    return boto3.resource("dynamodb", **_aws_kwargs())


def secretsmanager_client() -> Any:
    """Return a Secrets Manager client."""
    return boto3.client("secretsmanager", **_aws_kwargs())


def encode_token(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a pagination payload into an opaque cursor."""
    if not payload:
        return None
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode an opaque pagination cursor."""
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidTokenError("next_token is not a valid pagination cursor") from exc
    if not isinstance(payload, dict):
        raise InvalidTokenError("next_token is not a valid pagination cursor")
    return payload


def _sort_key(item: Dict[str, Any]) -> Tuple[str, str]:
    """Sort key used for newest-first ordering."""
    return (str(item.get("created_at", "")), str(item.get("message_id", "")))


class MessageRepository:
    """Interface for contact-message persistence."""

    def put_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a message item."""
        raise NotImplementedError

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Return a message by id or ``None``."""
        raise NotImplementedError

    def delete_message(self, message_id: str) -> bool:
        """Delete a message; return ``True`` when it existed."""
        raise NotImplementedError

    def list_messages(
        self, limit: int = 50, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of messages (newest first) and the next cursor."""
        raise NotImplementedError

    def health(self) -> bool:
        """Return ``True`` when the datastore is reachable."""
        raise NotImplementedError


class DynamoDBMessageRepository(MessageRepository):
    """DynamoDB-backed repository."""

    def __init__(self, table_name: Optional[str] = None, table: Any = None) -> None:
        self.table_name = table_name or os.environ.get("MESSAGES_TABLE", DEFAULT_TABLE_NAME)
        self._table = table

    @property
    def table(self) -> Any:
        """Lazily resolve the DynamoDB Table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    def put_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self.table.put_item(Item=item)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to store message: %s" % exc) from exc
        return item

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.table.get_item(Key={"message_id": message_id})
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to read message: %s" % exc) from exc
        item = response.get("Item")
        return dict(item) if item else None

    def delete_message(self, message_id: str) -> bool:
        try:
            response = self.table.delete_item(
                Key={"message_id": message_id},
                ReturnValues="ALL_OLD",
            )
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to delete message: %s" % exc) from exc
        return bool(response.get("Attributes"))

    def list_messages(
        self, limit: int = 50, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        scan_kwargs: Dict[str, Any] = {"Limit": max(1, int(limit))}
        start_key = decode_token(next_token)
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        try:
            response = self.table.scan(**scan_kwargs)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to list messages: %s" % exc) from exc
        items = [dict(item) for item in response.get("Items", [])]
        items.sort(key=_sort_key, reverse=True)
        return items, encode_token(response.get("LastEvaluatedKey"))

    def health(self) -> bool:
        try:
            self.table.scan(Limit=1)
        except (ClientError, BotoCoreError):
            return False
        return True


class InMemoryMessageRepository(MessageRepository):
    """In-memory repository used for tests and local development."""

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        for item in items or []:
            self._items[str(item["message_id"])] = dict(item)
        self.healthy = True

    def put_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        stored = dict(item)
        self._items[str(stored["message_id"])] = stored
        return dict(stored)

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(message_id)
        return dict(item) if item else None

    def delete_message(self, message_id: str) -> bool:
        return self._items.pop(message_id, None) is not None

    def list_messages(
        self, limit: int = 50, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        cursor = decode_token(next_token) or {}
        try:
            offset = int(cursor.get("offset", 0))
        except (TypeError, ValueError) as exc:
            raise InvalidTokenError("next_token is not a valid pagination cursor") from exc
        if offset < 0:
            raise InvalidTokenError("next_token is not a valid pagination cursor")
        ordered = sorted(self._items.values(), key=_sort_key, reverse=True)
        page = ordered[offset: offset + limit]
        token = None
        if offset + limit < len(ordered):
            token = encode_token({"offset": offset + limit})
        return [dict(item) for item in page], token

    def health(self) -> bool:
        return self.healthy
