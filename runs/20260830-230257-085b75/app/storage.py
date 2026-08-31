"""DynamoDB data access layer for the URL shortener service.

The application only depends on the small :class:`UrlRepository` interface,
which keeps the HTTP layer decoupled from AWS and makes it trivial to inject a
fake repository in tests.
"""
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3

DEFAULT_TABLE_NAME = "url_shortener_mappings"
DEFAULT_REGION = "us-east-1"


def aws_region() -> str:
    """Region used for every AWS client (defaults to us-east-1)."""
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or DEFAULT_REGION


def aws_endpoint_url() -> Optional[str]:
    """LocalStack-compatible endpoint override (None when unset)."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def table_name() -> str:
    """Name of the DynamoDB table holding the code -> URL mappings."""
    return (
        os.environ.get("MAPPINGS_TABLE_NAME")
        or os.environ.get("MAPPINGS_TABLE")
        or os.environ.get("DYNAMODB_TABLE")
        or DEFAULT_TABLE_NAME
    )


def dynamodb_resource():
    """Create a DynamoDB service resource honouring AWS_ENDPOINT_URL."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def normalize_item(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert DynamoDB Decimals to plain numbers and fill in defaults."""
    result: Dict[str, Any] = {}
    for key, value in (item or {}).items():
        if isinstance(value, Decimal):
            result[key] = int(value) if value == value.to_integral_value() else float(value)
        else:
            result[key] = value
    result.setdefault("visit_count", 0)
    result.setdefault("last_visited_at", None)
    return result


class UrlRepository:
    """Interface implemented by every storage backend."""

    def create(self, item: Dict[str, Any]) -> bool:
        """Store a new mapping; return False when the code already exists."""
        raise NotImplementedError

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        """Return the mapping for ``code`` or None."""
        raise NotImplementedError

    def increment_visit(self, code: str, timestamp: str) -> Optional[Dict[str, Any]]:
        """Atomically bump the visit counter; return the updated item or None."""
        raise NotImplementedError

    def list_items(self, limit: int, start_code: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of mappings plus the next start code (or None)."""
        raise NotImplementedError

    def delete(self, code: str) -> bool:
        """Delete a mapping; return False when the code does not exist."""
        raise NotImplementedError

    def health(self) -> str:
        """Return the backend status string, raising on failure."""
        raise NotImplementedError


class DynamoUrlRepository(UrlRepository):
    """DynamoDB implementation of :class:`UrlRepository`."""

    def __init__(self, name: Optional[str] = None, table: Any = None) -> None:
        self.table_name = name or table_name()
        self._table = table

    @property
    def table(self) -> Any:
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    def _conditional_failure(self) -> Any:
        return self.table.meta.client.exceptions.ConditionalCheckFailedException

    def create(self, item: Dict[str, Any]) -> bool:
        payload = dict(item)
        payload.setdefault("visit_count", 0)
        payload.setdefault("last_visited_at", None)
        try:
            self.table.put_item(Item=payload, ConditionExpression="attribute_not_exists(code)")
        except self._conditional_failure():
            return False
        return True

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key={"code": code})
        item = response.get("Item")
        if not item:
            return None
        return normalize_item(item)

    def increment_visit(self, code: str, timestamp: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.table.update_item(
                Key={"code": code},
                UpdateExpression="SET last_visited_at = :ts ADD visit_count :one",
                ExpressionAttributeValues={":ts": timestamp, ":one": 1},
                ConditionExpression="attribute_exists(code)",
                ReturnValues="ALL_NEW",
            )
        except self._conditional_failure():
            return None
        return normalize_item(response.get("Attributes"))

    def list_items(self, limit: int, start_code: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": int(limit)}
        if start_code:
            kwargs["ExclusiveStartKey"] = {"code": start_code}
        response = self.table.scan(**kwargs)
        items = [normalize_item(raw) for raw in response.get("Items", [])]
        next_key = (response.get("LastEvaluatedKey") or {}).get("code")
        return items, next_key

    def delete(self, code: str) -> bool:
        try:
            self.table.delete_item(
                Key={"code": code},
                ConditionExpression="attribute_exists(code)",
            )
        except self._conditional_failure():
            return False
        return True

    def health(self) -> str:
        response = self.table.meta.client.describe_table(TableName=self.table_name)
        return str(response["Table"]["TableStatus"])


class InMemoryUrlRepository(UrlRepository):
    """Process-local repository, useful for local development and tests."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}

    def create(self, item: Dict[str, Any]) -> bool:
        code = str(item["code"])
        if code in self.items:
            return False
        stored = dict(item)
        stored.setdefault("visit_count", 0)
        stored.setdefault("last_visited_at", None)
        self.items[code] = stored
        return True

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        item = self.items.get(code)
        return dict(item) if item else None

    def increment_visit(self, code: str, timestamp: str) -> Optional[Dict[str, Any]]:
        item = self.items.get(code)
        if not item:
            return None
        item["visit_count"] = int(item.get("visit_count") or 0) + 1
        item["last_visited_at"] = timestamp
        return dict(item)

    def list_items(self, limit: int, start_code: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        codes = sorted(self.items)
        if start_code and start_code in codes:
            codes = codes[codes.index(start_code) + 1:]
        elif start_code:
            codes = [code for code in codes if code > start_code]
        page = codes[: int(limit)]
        next_key = page[-1] if len(codes) > len(page) and page else None
        return [dict(self.items[code]) for code in page], next_key

    def delete(self, code: str) -> bool:
        return self.items.pop(code, None) is not None

    def health(self) -> str:
        return "ACTIVE"
