"""Data access layer for the contact-form backend.

Holds the boto3 clients (DynamoDB, Secrets Manager) behind small interfaces so
the FastAPI application never talks to AWS directly and can be tested with an
in-memory repository.
"""

import base64
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3

DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE_NAME = "contact-form-messages"
DEFAULT_SECRET_NAME = "contact-form/admin-api-key"  # nosec B105 - resource name, not a credential
SECRET_KEY_CANDIDATES = ("api_key", "admin_api_key", "ADMIN_API_KEY", "key", "value")


def aws_region() -> str:
    """Region used for every AWS client."""
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or DEFAULT_REGION


def aws_endpoint_url() -> Optional[str]:
    """Optional endpoint override (LocalStack compatibility)."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource():
    """Build a DynamoDB service resource."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def secretsmanager_client():
    """Build a Secrets Manager client."""
    return boto3.client(
        "secretsmanager",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def messages_table_name() -> str:
    """Name of the DynamoDB messages table."""
    return os.environ.get("MESSAGES_TABLE_NAME", DEFAULT_TABLE_NAME)


def admin_secret_name() -> str:
    """Secrets Manager id holding the admin API key."""
    return os.environ.get("ADMIN_API_KEY_SECRET_NAME", DEFAULT_SECRET_NAME)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_message_id() -> str:
    """Generate a server-side message id."""
    return str(uuid.uuid4())


def encode_cursor(key: Dict[str, Any]) -> str:
    """Encode a DynamoDB LastEvaluatedKey into an opaque cursor."""
    raw = json.dumps(key, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> Dict[str, Any]:
    """Decode an opaque cursor back into a DynamoDB ExclusiveStartKey."""
    padded = cursor + "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError("cursor must decode to a non-empty object")
    return data


def _normalise_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure every expected attribute is present on a stored item."""
    return {
        "message_id": str(item.get("message_id", "")),
        "name": str(item.get("name", "")),
        "email": str(item.get("email", "")),
        "message": str(item.get("message", "")),
        "created_at": str(item.get("created_at", "")),
        "source_ip": item.get("source_ip"),
    }


class MessageRepository:
    """Interface implemented by the message stores."""

    def put_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def list_messages(
        self,
        limit: int = 50,
        cursor: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        raise NotImplementedError

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def delete_message(self, message_id: str) -> bool:
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:
        raise NotImplementedError


class DynamoDBMessageRepository(MessageRepository):
    """DynamoDB backed implementation of the message repository."""

    def __init__(self, table_name: Optional[str] = None, table: Any = None) -> None:
        self._table_name = table_name
        self._table = table

    @property
    def table_name(self) -> str:
        return self._table_name or messages_table_name()

    def _get_table(self):
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    def put_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        stored = _normalise_item(item)
        payload = {key: value for key, value in stored.items() if value is not None}
        self._get_table().put_item(Item=payload)
        return stored

    def list_messages(
        self,
        limit: int = 50,
        cursor: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        kwargs: Dict[str, Any] = {"Limit": limit}
        if cursor:
            kwargs["ExclusiveStartKey"] = cursor
        response = self._get_table().scan(**kwargs)
        items = [_normalise_item(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey") or None
        return items, last_key

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        response = self._get_table().get_item(Key={"message_id": message_id})
        item = response.get("Item")
        if not item:
            return None
        return _normalise_item(item)

    def delete_message(self, message_id: str) -> bool:
        response = self._get_table().delete_item(
            Key={"message_id": message_id},
            ReturnValues="ALL_OLD",
        )
        return bool(response.get("Attributes"))

    def health(self) -> Dict[str, Any]:
        try:
            status = self._get_table().table_status
            return {"table": self.table_name, "reachable": True, "table_status": status}
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return {"table": self.table_name, "reachable": False, "error": str(exc)}


class InMemoryMessageRepository(MessageRepository):
    """In-memory repository used for tests and local development."""

    def __init__(self, name: str = "in-memory", reachable: bool = True) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        self._name = name
        self._reachable = reachable

    def put_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        stored = _normalise_item(item)
        self._items[stored["message_id"]] = dict(stored)
        return stored

    def list_messages(
        self,
        limit: int = 50,
        cursor: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        items = list(self._items.values())
        start = 0
        if cursor:
            last_id = cursor.get("message_id")
            for index, item in enumerate(items):
                if item["message_id"] == last_id:
                    start = index + 1
                    break
        page = [dict(item) for item in items[start:start + limit]]
        last_key = None
        if page and start + limit < len(items):
            last_key = {"message_id": page[-1]["message_id"]}
        return page, last_key

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(message_id)
        return dict(item) if item else None

    def delete_message(self, message_id: str) -> bool:
        return self._items.pop(message_id, None) is not None

    def health(self) -> Dict[str, Any]:
        return {"table": self._name, "reachable": self._reachable}


def _extract_secret_value(raw: str) -> str:
    """Pull the API key out of a Secrets Manager secret string."""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(parsed, dict):
        for candidate in SECRET_KEY_CANDIDATES:
            if parsed.get(candidate):
                return str(parsed[candidate])
        return ""
    return str(parsed)


class AdminKeyProvider:
    """Loads and caches the shared administrator API key.

    The value comes from the ADMIN_API_KEY environment variable when present,
    otherwise from AWS Secrets Manager.
    """

    def __init__(self, secret_id: Optional[str] = None, client: Any = None) -> None:
        self._secret_id = secret_id
        self._client = client
        self._cached: Optional[str] = None

    @property
    def secret_id(self) -> str:
        return self._secret_id or admin_secret_name()

    def _get_client(self):
        if self._client is None:
            self._client = secretsmanager_client()
        return self._client

    def invalidate(self) -> None:
        """Drop the cached key so the next call reloads it."""
        self._cached = None

    def get_key(self) -> str:
        env_key = os.environ.get("ADMIN_API_KEY")
        if env_key:
            return env_key
        if self._cached:
            return self._cached
        response = self._get_client().get_secret_value(SecretId=self.secret_id)
        value = _extract_secret_value(response.get("SecretString", ""))
        self._cached = value
        return value
