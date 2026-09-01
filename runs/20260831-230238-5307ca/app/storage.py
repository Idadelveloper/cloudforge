"""Data access helpers for the bookmark manager API.

All AWS clients honour the AWS_ENDPOINT_URL environment variable so the same
code can target LocalStack or real AWS.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

logger = logging.getLogger("bookmark_manager_api.storage")

DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE_NAME = "bookmarks"
DEFAULT_TAG_INDEX = "bookmarks-tag-index"
DEFAULT_SECRET_NAME = "bookmark-manager/api-key"
DEFAULT_SECRET_TTL_SECONDS = 300
SECRET_KEY_CANDIDATES = ("api_key", "apiKey", "API_KEY", "value", "secret")


def _aws_kwargs() -> Dict[str, Any]:
    """Common keyword arguments for every boto3 client/resource."""
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )
    return {
        "region_name": region,
        "endpoint_url": os.environ.get("AWS_ENDPOINT_URL") or None,
    }


def dynamodb_resource():
    """Build a DynamoDB resource pointing at LocalStack when configured."""
    return boto3.resource("dynamodb", **_aws_kwargs())


def secretsmanager_client():
    """Build a Secrets Manager client pointing at LocalStack when configured."""
    return boto3.client("secretsmanager", **_aws_kwargs())


class BookmarkRepository:
    """Small interface over the DynamoDB bookmarks table."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        tag_index: Optional[str] = None,
        resource_factory: Callable[[], Any] = dynamodb_resource,
    ) -> None:
        self.table_name = table_name or os.environ.get("BOOKMARKS_TABLE", DEFAULT_TABLE_NAME)
        self.tag_index = tag_index or os.environ.get("BOOKMARKS_TAG_INDEX", DEFAULT_TAG_INDEX)
        self._resource_factory = resource_factory
        self._table: Any = None
        self._lock = threading.Lock()

    @property
    def table(self) -> Any:
        """Lazily resolve the DynamoDB table object."""
        if self._table is None:
            with self._lock:
                if self._table is None:
                    self._table = self._resource_factory().Table(self.table_name)
        return self._table

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a bookmark item and return it."""
        self.table.put_item(Item=item)
        return item

    def get(self, bookmark_id: str) -> Optional[Dict[str, Any]]:
        """Return a single bookmark or None when it does not exist."""
        response = self.table.get_item(Key={"bookmark_id": bookmark_id})
        return response.get("Item")

    def delete(self, bookmark_id: str) -> bool:
        """Delete a bookmark; return False when the id is unknown."""
        try:
            self.table.delete_item(
                Key={"bookmark_id": bookmark_id},
                ConditionExpression=Attr("bookmark_id").exists(),
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def list_bookmarks(self, tag: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """List bookmarks, optionally keeping only those carrying ``tag``.

        A filtered scan is used so that bookmarks holding the tag in any
        position of their tag list are returned (the GSI only projects the
        primary tag).
        """
        scan_kwargs: Dict[str, Any] = {}
        if tag:
            scan_kwargs["FilterExpression"] = Attr("tags").contains(tag)

        items: List[Dict[str, Any]] = []
        while True:
            response = self.table.scan(**scan_kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key or len(items) >= limit:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key

        items.sort(key=lambda entry: str(entry.get("created_at", "")), reverse=True)
        return items[:limit]


class ApiKeyProvider:
    """Fetches and caches the shared API key from AWS Secrets Manager."""

    def __init__(
        self,
        secret_name: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        client_factory: Callable[[], Any] = secretsmanager_client,
    ) -> None:
        self.secret_name = secret_name or os.environ.get("API_KEY_SECRET_NAME", DEFAULT_SECRET_NAME)
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else int(
            os.environ.get("API_KEY_CACHE_TTL", str(DEFAULT_SECRET_TTL_SECONDS))
        )
        self._client_factory = client_factory
        self._cached_key: Optional[str] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get_api_key(self, force_refresh: bool = False) -> Optional[str]:
        """Return the expected API key, refreshing the cache when needed."""
        now = time.monotonic()
        with self._lock:
            if not force_refresh and self._cached_key and now < self._expires_at:
                return self._cached_key

            key = self._fetch_secret()
            if key is None:
                key = os.environ.get("BOOKMARK_API_KEY") or None
                if key:
                    logger.warning("Falling back to the BOOKMARK_API_KEY environment variable")

            self._cached_key = key
            self._expires_at = time.monotonic() + max(self.ttl_seconds, 1)
            return self._cached_key

    def _fetch_secret(self) -> Optional[str]:
        try:
            client = self._client_factory()
            response = client.get_secret_value(SecretId=self.secret_name)
        except Exception:
            logger.warning("Could not read secret %s from Secrets Manager", self.secret_name)
            return None
        return self._extract_key(response)

    @staticmethod
    def _extract_key(response: Dict[str, Any]) -> Optional[str]:
        raw = response.get("SecretString")
        if raw is None:
            binary = response.get("SecretBinary")
            if isinstance(binary, (bytes, bytearray)):
                raw = bytes(binary).decode("utf-8", "ignore")
        if not raw:
            return None

        raw = raw.strip()
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return raw or None

        if isinstance(parsed, dict):
            for candidate in SECRET_KEY_CANDIDATES:
                value = parsed.get(candidate)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None
        if isinstance(parsed, str):
            return parsed.strip() or None
        return raw or None
