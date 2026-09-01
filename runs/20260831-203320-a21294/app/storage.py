"""Data-access layer for the contact-form backend.

The application only depends on the small :class:`MessageRepository`
interface, which keeps the HTTP layer decoupled from DynamoDB and makes the
service trivially testable with an in-memory fake.
"""

import base64
import binascii
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import boto3

LOGGER = logging.getLogger("contact_form_backend.storage")

DEFAULT_TABLE_NAME = "contact-messages"
DEFAULT_REGION = "us-east-1"


class InvalidPaginationToken(ValueError):
    """Raised when a caller supplies an undecodable ``next_token``."""


def table_name() -> str:
    """Name of the DynamoDB table holding contact messages."""
    return os.environ.get("TABLE_NAME") or DEFAULT_TABLE_NAME


def aws_region() -> str:
    """Region for AWS clients (LocalStack friendly)."""
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )


def dynamodb_resource():
    """Build a DynamoDB resource honouring AWS_ENDPOINT_URL when present."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def encode_token(key: Dict[str, Any]) -> str:
    """Encode a DynamoDB LastEvaluatedKey into an opaque string."""
    raw = json.dumps(key, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: str) -> Dict[str, Any]:
    """Decode an opaque pagination token back into a DynamoDB key."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidPaginationToken("next_token is not a valid pagination token") from exc
    if not isinstance(data, dict):
        raise InvalidPaginationToken("next_token is not a valid pagination token")
    return data


class MessageRepository:
    """Storage interface used by the HTTP layer."""

    def create_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a message and return the stored representation."""
        raise NotImplementedError

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Return a single message or ``None`` when absent."""
        raise NotImplementedError

    def list_messages(
        self,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of messages (newest first) and the next token."""
        raise NotImplementedError

    def delete_message(self, message_id: str) -> bool:
        """Delete a message; ``True`` when something was removed."""
        raise NotImplementedError

    def healthy(self) -> bool:
        """Return ``True`` when the backing store is reachable."""
        raise NotImplementedError


class DynamoDBMessageRepository(MessageRepository):
    """DynamoDB-backed implementation of :class:`MessageRepository`."""

    def __init__(self, table_name: Optional[str] = None, table: Any = None) -> None:
        self._table_name = table_name or os.environ.get("TABLE_NAME") or DEFAULT_TABLE_NAME
        self._table = table

    @property
    def table_name(self) -> str:
        """Name of the underlying DynamoDB table."""
        return self._table_name

    @property
    def table(self) -> Any:
        """Lazily created boto3 Table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(self._table_name)
        return self._table

    def create_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        stored = {key: value for key, value in item.items() if value is not None}
        self.table.put_item(Item=stored)
        return dict(item)

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key={"id": message_id})
        item = response.get("Item")
        return dict(item) if item else None

    def list_messages(
        self,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": max(1, int(limit))}
        if next_token:
            kwargs["ExclusiveStartKey"] = decode_token(next_token)
        response = self.table.scan(**kwargs)
        items = [dict(item) for item in response.get("Items", [])]
        items.sort(key=lambda entry: str(entry.get("created_at", "")), reverse=True)
        last_key = response.get("LastEvaluatedKey")
        token = encode_token(dict(last_key)) if last_key else None
        return items, token

    def delete_message(self, message_id: str) -> bool:
        response = self.table.delete_item(
            Key={"id": message_id},
            ReturnValues="ALL_OLD",
        )
        return bool(response.get("Attributes"))

    def healthy(self) -> bool:
        try:
            self.table.load()
            return True
        except Exception as exc:  # pragma: no cover - depends on AWS state
            LOGGER.warning("DynamoDB table %s unreachable: %s", self._table_name, exc)
            return False


class InMemoryMessageRepository(MessageRepository):
    """Dependency-free repository, handy for local runs and tests."""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}

    def create_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        self._items[str(item["id"])] = dict(item)
        return dict(item)

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(message_id)
        return dict(item) if item else None

    def list_messages(
        self,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        ordered = sorted(
            self._items.values(),
            key=lambda entry: str(entry.get("created_at", "")),
            reverse=True,
        )
        offset = 0
        if next_token:
            data = decode_token(next_token)
            try:
                offset = int(data.get("offset", 0))
            except (TypeError, ValueError) as exc:
                raise InvalidPaginationToken("next_token is not a valid pagination token") from exc
        page = ordered[offset:offset + limit]
        token = None
        if offset + limit < len(ordered):
            token = encode_token({"offset": offset + limit})
        return [dict(entry) for entry in page], token

    def delete_message(self, message_id: str) -> bool:
        return self._items.pop(message_id, None) is not None

    def healthy(self) -> bool:
        return True
