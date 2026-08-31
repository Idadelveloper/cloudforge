"""Data access layer for the url_shortener service.

A tiny repository interface hides DynamoDB behind plain Python calls so the
API layer (and the tests) can swap in an in-memory implementation.
"""

import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import boto3

DEFAULT_TABLE_NAME = "url_shortener_urls"


class StorageError(Exception):
    """Generic storage failure."""


class NotFoundError(StorageError):
    """Raised when the requested short code does not exist."""


class CodeAlreadyExistsError(StorageError):
    """Raised when the short code is already taken."""


def aws_region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def table_name() -> str:
    return os.environ.get("URL_TABLE_NAME", DEFAULT_TABLE_NAME)


def dynamodb_resource():
    """Build a DynamoDB resource honouring AWS_ENDPOINT_URL (LocalStack)."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def clean_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a stored item (DynamoDB numbers arrive as Decimal)."""
    return {
        "code": str(raw.get("code", "")),
        "long_url": str(raw.get("long_url", "")),
        "created_at": raw.get("created_at") or "",
        "visit_count": int(raw.get("visit_count") or 0),
        "last_visited_at": raw.get("last_visited_at") or None,
    }


class UrlRepository:
    """Interface implemented by the concrete repositories below."""

    def create(self, code: str, long_url: str, created_at: str) -> Dict[str, Any]:
        raise NotImplementedError

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def register_visit(self, code: str, visited_at: str) -> Dict[str, Any]:
        raise NotImplementedError

    def delete(self, code: str) -> None:
        raise NotImplementedError

    def list_urls(
        self,
        limit: int = 25,
        start_after: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        raise NotImplementedError

    def healthy(self) -> bool:
        raise NotImplementedError


class DynamoUrlRepository(UrlRepository):
    """DynamoDB backed repository for short-code mappings."""

    def __init__(self, table: Any = None, table_name_override: Optional[str] = None) -> None:
        self._table_name = table_name_override or table_name()
        self._table = table

    @property
    def table(self) -> Any:
        if self._table is None:
            self._table = dynamodb_resource().Table(self._table_name)
        return self._table

    @property
    def _client(self) -> Any:
        return self.table.meta.client

    def create(self, code: str, long_url: str, created_at: str) -> Dict[str, Any]:
        item = {
            "code": code,
            "long_url": long_url,
            "created_at": created_at,
            "visit_count": 0,
        }
        errors = self._client.exceptions
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(code)")
        except errors.ConditionalCheckFailedException as exc:
            raise CodeAlreadyExistsError(code) from exc
        except errors.ClientError as exc:
            raise StorageError(str(exc)) from exc
        return clean_item(item)

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        errors = self._client.exceptions
        try:
            response = self.table.get_item(Key={"code": code})
        except errors.ClientError as exc:
            raise StorageError(str(exc)) from exc
        raw = response.get("Item")
        if not raw:
            return None
        return clean_item(raw)

    def register_visit(self, code: str, visited_at: str) -> Dict[str, Any]:
        errors = self._client.exceptions
        try:
            response = self.table.update_item(
                Key={"code": code},
                UpdateExpression="SET last_visited_at = :ts ADD visit_count :inc",
                ConditionExpression="attribute_exists(code)",
                ExpressionAttributeValues={":ts": visited_at, ":inc": 1},
                ReturnValues="ALL_NEW",
            )
        except errors.ConditionalCheckFailedException as exc:
            raise NotFoundError(code) from exc
        except errors.ClientError as exc:
            raise StorageError(str(exc)) from exc
        return clean_item(response.get("Attributes") or {"code": code})

    def delete(self, code: str) -> None:
        errors = self._client.exceptions
        try:
            self.table.delete_item(
                Key={"code": code},
                ConditionExpression="attribute_exists(code)",
            )
        except errors.ConditionalCheckFailedException as exc:
            raise NotFoundError(code) from exc
        except errors.ClientError as exc:
            raise StorageError(str(exc)) from exc

    def list_urls(
        self,
        limit: int = 25,
        start_after: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": max(1, min(int(limit), 100))}
        if start_after:
            kwargs["ExclusiveStartKey"] = {"code": start_after}
        errors = self._client.exceptions
        try:
            response = self.table.scan(**kwargs)
        except errors.ClientError as exc:
            raise StorageError(str(exc)) from exc
        items = [clean_item(raw) for raw in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey") or {}
        return items, last_key.get("code")

    def healthy(self) -> bool:
        try:
            response = self._client.describe_table(TableName=self._table_name)
        except Exception:  # noqa: BLE001 - any failure means "not reachable"
            return False
        status = str((response.get("Table") or {}).get("TableStatus", ""))
        return status.upper() == "ACTIVE"


class InMemoryUrlRepository(UrlRepository):
    """Thread-safe in-memory repository, used by tests and local runs."""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, code: str, long_url: str, created_at: str) -> Dict[str, Any]:
        with self._lock:
            if code in self._items:
                raise CodeAlreadyExistsError(code)
            item = clean_item(
                {
                    "code": code,
                    "long_url": long_url,
                    "created_at": created_at,
                    "visit_count": 0,
                }
            )
            self._items[code] = item
            return dict(item)

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._items.get(code)
            return dict(item) if item else None

    def register_visit(self, code: str, visited_at: str) -> Dict[str, Any]:
        with self._lock:
            item = self._items.get(code)
            if item is None:
                raise NotFoundError(code)
            item["visit_count"] = int(item.get("visit_count") or 0) + 1
            item["last_visited_at"] = visited_at
            return dict(item)

    def delete(self, code: str) -> None:
        with self._lock:
            if code not in self._items:
                raise NotFoundError(code)
            del self._items[code]

    def list_urls(
        self,
        limit: int = 25,
        start_after: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        with self._lock:
            codes = sorted(self._items)
            if start_after:
                codes = [code for code in codes if code > start_after]
            page_size = max(1, min(int(limit), 100))
            page = codes[:page_size]
            items = [dict(self._items[code]) for code in page]
            next_code = page[-1] if len(codes) > page_size and page else None
            return items, next_code

    def healthy(self) -> bool:
        return True
