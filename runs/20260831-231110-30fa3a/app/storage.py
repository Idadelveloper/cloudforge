"""Data-access layer for the bookmark manager API.

Everything that talks to AWS lives here behind small interfaces so the HTTP
layer can be tested with an in-memory repository and a static key provider.
"""

import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import boto3

LOGGER = logging.getLogger("bookmark_manager_api.storage")

DEFAULT_REGION = "us-east-1"
DEFAULT_BOOKMARKS_TABLE = "bookmarks"
DEFAULT_TAGS_TABLE = "bookmark_tags"
DEFAULT_APIKEY_STORE_ID = "bookmark-manager/api-key"


class TokenError(ValueError):
    """Raised when an opaque pagination cursor cannot be decoded."""


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def region_name() -> str:
    """AWS region used by every client."""
    return _env("AWS_DEFAULT_REGION", DEFAULT_REGION)


def endpoint_url() -> Optional[str]:
    """Optional AWS endpoint override (LocalStack)."""
    value = os.environ.get("AWS_ENDPOINT_URL")
    return value.strip() if value and value.strip() else None


def bookmarks_table_name() -> str:
    """Name of the primary bookmarks table."""
    return _env("BOOKMARKS_TABLE", DEFAULT_BOOKMARKS_TABLE)


def tags_table_name() -> str:
    """Name of the tag lookup table."""
    return _env("BOOKMARK_TAGS_TABLE", DEFAULT_TAGS_TABLE)


def api_key_store_id() -> str:
    """Secrets Manager identifier holding the shared API key."""
    return _env("API_KEY_SECRET_ID", DEFAULT_APIKEY_STORE_ID)


def dynamodb_resource():
    """Create a DynamoDB resource honouring AWS_ENDPOINT_URL."""
    return boto3.resource(
        "dynamodb",
        region_name=region_name(),
        endpoint_url=endpoint_url(),
    )


def secretsmanager_client():
    """Create a Secrets Manager client honouring AWS_ENDPOINT_URL."""
    return boto3.client(
        "secretsmanager",
        region_name=region_name(),
        endpoint_url=endpoint_url(),
    )


def encode_token(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a pagination cursor as an opaque base64 string."""
    if not payload:
        return None
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode a cursor produced by :func:`encode_token`."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise TokenError("next_token is not a valid pagination cursor") from exc
    if not isinstance(payload, dict):
        raise TokenError("next_token is not a valid pagination cursor")
    return payload


def normalise_item(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Coerce a raw DynamoDB item into the API representation."""
    if not item:
        return None
    tags = item.get("tags") or []
    if isinstance(tags, (set, frozenset)):
        tags = sorted(tags)
    if isinstance(tags, str):
        tags = [tags]
    created_at = str(item.get("created_at", ""))
    return {
        "bookmark_id": str(item.get("bookmark_id", "")),
        "url": str(item.get("url", "")),
        "title": str(item.get("title", "")),
        "tags": [str(tag) for tag in tags],
        "created_at": created_at,
        "updated_at": str(item.get("updated_at", created_at)),
    }


def _sort_newest_first(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        items,
        key=lambda entry: (entry.get("created_at", ""), entry.get("bookmark_id", "")),
        reverse=True,
    )


class BookmarkRepository:
    """Interface implemented by every bookmark store."""

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a bookmark and return the stored representation."""
        raise NotImplementedError

    def get(self, bookmark_id: str) -> Optional[Dict[str, Any]]:
        """Return a bookmark or ``None``."""
        raise NotImplementedError

    def delete(self, bookmark_id: str) -> Optional[Dict[str, Any]]:
        """Delete a bookmark, returning the removed item or ``None``."""
        raise NotImplementedError

    def list_bookmarks(
        self, limit: int, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of bookmarks plus the next cursor."""
        raise NotImplementedError

    def list_by_tag(
        self, tag: str, limit: int, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of bookmarks carrying ``tag``."""
        raise NotImplementedError

    def health_check(self) -> bool:
        """Return ``True`` when the backing store is reachable."""
        raise NotImplementedError


class InMemoryBookmarkRepository(BookmarkRepository):
    """Dependency-free repository used by tests and local experiments."""

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        for item in items or []:
            normalised = normalise_item(item)
            if normalised:
                self._items[normalised["bookmark_id"]] = normalised
        self.healthy = True

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        normalised = normalise_item(item) or {}
        self._items[normalised["bookmark_id"]] = normalised
        return dict(normalised)

    def get(self, bookmark_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(bookmark_id)
        return dict(item) if item else None

    def delete(self, bookmark_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.pop(bookmark_id, None)
        return dict(item) if item else None

    def list_bookmarks(
        self, limit: int, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        return self._page(_sort_newest_first(list(self._items.values())), limit, next_token)

    def list_by_tag(
        self, tag: str, limit: int, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        wanted = tag.strip().lower()
        matching = [
            item
            for item in self._items.values()
            if wanted in [str(existing).lower() for existing in item.get("tags", [])]
        ]
        return self._page(_sort_newest_first(matching), limit, next_token)

    @staticmethod
    def _page(
        items: List[Dict[str, Any]], limit: int, next_token: Optional[str]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        offset = 0
        payload = decode_token(next_token)
        if payload is not None:
            try:
                offset = max(0, int(payload.get("offset", 0)))
            except (TypeError, ValueError) as exc:
                raise TokenError("next_token is not a valid pagination cursor") from exc
        page = [dict(item) for item in items[offset:offset + limit]]
        token = encode_token({"offset": offset + limit}) if offset + limit < len(items) else None
        return page, token

    def health_check(self) -> bool:
        return bool(self.healthy)


class DynamoBookmarkRepository(BookmarkRepository):
    """DynamoDB-backed repository using two tables (items and tag index)."""

    def __init__(
        self,
        resource: Any = None,
        bookmarks_table: Optional[str] = None,
        tags_table: Optional[str] = None,
    ) -> None:
        self._resource = resource if resource is not None else dynamodb_resource()
        self._bookmarks_name = bookmarks_table or bookmarks_table_name()
        self._tags_name = tags_table or tags_table_name()
        self._bookmarks = self._resource.Table(self._bookmarks_name)
        self._tags = self._resource.Table(self._tags_name)

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        normalised = normalise_item(item) or {}
        self._bookmarks.put_item(Item=dict(normalised))
        for tag in normalised.get("tags", []):
            self._tags.put_item(
                Item={
                    "tag": tag,
                    "bookmark_id": normalised["bookmark_id"],
                    "url": normalised["url"],
                    "title": normalised["title"],
                    "created_at": normalised["created_at"],
                }
            )
        return dict(normalised)

    def get(self, bookmark_id: str) -> Optional[Dict[str, Any]]:
        response = self._bookmarks.get_item(Key={"bookmark_id": bookmark_id})
        return normalise_item(response.get("Item"))

    def delete(self, bookmark_id: str) -> Optional[Dict[str, Any]]:
        existing = self.get(bookmark_id)
        if not existing:
            return None
        self._bookmarks.delete_item(Key={"bookmark_id": bookmark_id})
        for tag in existing.get("tags", []):
            self._tags.delete_item(Key={"tag": tag, "bookmark_id": bookmark_id})
        return existing

    def list_bookmarks(
        self, limit: int, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": limit}
        start_key = decode_token(next_token)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = self._bookmarks.scan(**kwargs)
        items = [normalise_item(raw) or {} for raw in response.get("Items", [])]
        return _sort_newest_first(items), encode_token(response.get("LastEvaluatedKey"))

    def list_by_tag(
        self, tag: str, limit: int, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": "#tagname = :tagvalue",
            "ExpressionAttributeNames": {"#tagname": "tag"},
            "ExpressionAttributeValues": {":tagvalue": tag},
            "Limit": limit,
            "ScanIndexForward": False,
        }
        start_key = decode_token(next_token)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = self._tags.query(**kwargs)
        items: List[Dict[str, Any]] = []
        for entry in response.get("Items", []):
            bookmark_id = entry.get("bookmark_id")
            if not bookmark_id:
                continue
            full = self.get(str(bookmark_id))
            if full:
                items.append(full)
                continue
            fallback = normalise_item(
                {
                    "bookmark_id": bookmark_id,
                    "url": entry.get("url", ""),
                    "title": entry.get("title", ""),
                    "tags": [tag],
                    "created_at": entry.get("created_at", ""),
                }
            )
            if fallback:
                items.append(fallback)
        return _sort_newest_first(items), encode_token(response.get("LastEvaluatedKey"))

    def health_check(self) -> bool:
        try:
            client = self._resource.meta.client
            client.describe_table(TableName=self._bookmarks_name)
            return True
        except Exception as exc:
            LOGGER.warning("DynamoDB health check failed: %s", exc)
            return False


def parse_api_key_payload(raw: str) -> Optional[str]:
    """Extract the API key from a Secrets Manager payload.

    Accepts JSON documents such as ``{"api_key": "..."}`` as well as plain
    string secrets.
    """
    if not raw:
        return None
    text = raw.strip()
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return text or None
    if isinstance(payload, dict):
        for field in ("api_key", "apiKey", "API_KEY", "value"):
            candidate = payload.get(field)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
    if isinstance(payload, str):
        return payload.strip() or None
    return None


class StaticApiKeyProvider:
    """Provider backed by a fixed value (tests / local development)."""

    def __init__(self, value: Optional[str]) -> None:
        self._value = value

    def get_api_key(self, force_refresh: bool = False) -> Optional[str]:
        """Return the configured value."""
        del force_refresh
        return self._value


class SecretsManagerApiKeyProvider:
    """Fetch (and cache) the shared API key from AWS Secrets Manager."""

    def __init__(self, client: Any = None, store_id: Optional[str] = None) -> None:
        self._client = client
        self._store_id = store_id or api_key_store_id()
        self._cached: Optional[str] = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = secretsmanager_client()
        return self._client

    def get_api_key(self, force_refresh: bool = False) -> Optional[str]:
        """Return the expected API key, refreshing the cache when asked."""
        if self._cached and not force_refresh:
            return self._cached
        try:
            response = self._get_client().get_secret_value(SecretId=self._store_id)
        except Exception as exc:
            LOGGER.warning("Unable to read API key from Secrets Manager (%s): %s", self._store_id, exc)
            return self._cached or self._fallback()
        value = parse_api_key_payload(str(response.get("SecretString") or ""))
        if value:
            self._cached = value
            return value
        LOGGER.warning("Secret %s did not contain an api_key value", self._store_id)
        return self._cached or self._fallback()

    @staticmethod
    def _fallback() -> Optional[str]:
        value = os.environ.get("API_KEY")
        return value.strip() if value and value.strip() else None
