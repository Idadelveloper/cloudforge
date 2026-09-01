"""Data access layer for the shop inventory API.

The application only ever talks to this module; the DynamoDB implementation is
hidden behind :class:`ProductRepository` so tests can inject an in-memory
repository and run completely offline.
"""

import base64
import binascii
import json
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_TABLE_NAME = "shop-inventory-products"
DEFAULT_REGION = "us-east-1"
CONDITIONAL_FAILED = "ConditionalCheckFailedException"


class StorageError(Exception):
    """Generic, non-recoverable storage failure."""


class ProductAlreadyExists(StorageError):
    """Raised when a SKU already exists."""


class ProductNotFound(StorageError):
    """Raised when a SKU does not exist."""


class InsufficientStock(StorageError):
    """Raised when an adjustment would drive quantity below zero."""


class InvalidCursor(StorageError):
    """Raised when a pagination cursor cannot be decoded."""


def table_name() -> str:
    """Return the configured DynamoDB table name."""
    return os.environ.get("DYNAMODB_TABLE_NAME", DEFAULT_TABLE_NAME)


def aws_region() -> str:
    """Return the configured AWS region."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def dynamodb_resource():
    """Create a boto3 DynamoDB resource honouring AWS_ENDPOINT_URL (LocalStack)."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def encode_cursor(key: Dict[str, Any]) -> str:
    """Encode a DynamoDB LastEvaluatedKey as an opaque cursor."""
    raw = json.dumps(key, default=str, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> Dict[str, Any]:
    """Decode an opaque cursor back into a DynamoDB ExclusiveStartKey."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        key = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursor("Malformed pagination cursor") from exc
    if not isinstance(key, dict) or "sku" not in key:
        raise InvalidCursor("Malformed pagination cursor")
    return {"sku": str(key["sku"])}


def _error_code(exc: ClientError) -> str:
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    return str(error.get("Code", ""))


def to_product(item: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a raw DynamoDB item into plain python types."""
    return {
        "sku": str(item.get("sku", "")),
        "name": str(item.get("name", "")),
        "price": Decimal(str(item.get("price", "0"))),
        "quantity": int(item.get("quantity", 0)),
        "created_at": str(item.get("created_at", "")),
        "updated_at": str(item.get("updated_at", "")),
    }


class ProductRepository(ABC):
    """Interface used by the API layer."""

    @abstractmethod
    def ping(self) -> bool:
        """Return True when the backing store is reachable."""

    @abstractmethod
    def create_product(self, sku: str, name: str, price: Decimal, quantity: int) -> Dict[str, Any]:
        """Create and return a product; raise ProductAlreadyExists on duplicates."""

    @abstractmethod
    def get_product(self, sku: str) -> Optional[Dict[str, Any]]:
        """Return a product or None."""

    @abstractmethod
    def list_products(
        self, limit: int = 50, cursor: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of products and the next cursor."""

    @abstractmethod
    def adjust_stock(self, sku: str, delta: int) -> Dict[str, Any]:
        """Apply a signed delta atomically and return the updated product."""


class DynamoProductRepository(ProductRepository):
    """DynamoDB backed repository."""

    def __init__(self, name: Optional[str] = None, table: Any = None) -> None:
        self._name = name or table_name()
        self._table = table

    @property
    def name(self) -> str:
        """Table name in use."""
        return self._name

    @property
    def table(self) -> Any:
        """Lazily created boto3 Table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(self._name)
        return self._table

    def ping(self) -> bool:
        try:
            self.table.meta.client.describe_table(TableName=self._name)
            return True
        except (ClientError, BotoCoreError, ValueError):
            return False

    def create_product(self, sku: str, name: str, price: Decimal, quantity: int) -> Dict[str, Any]:
        now = utc_now()
        item = {
            "sku": sku,
            "name": name,
            "price": Decimal(str(price)),
            "quantity": int(quantity),
            "created_at": now,
            "updated_at": now,
        }
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(#sku)",
                ExpressionAttributeNames={"#sku": "sku"},
            )
        except ClientError as exc:
            if _error_code(exc) == CONDITIONAL_FAILED:
                raise ProductAlreadyExists(f"Product with sku '{sku}' already exists") from exc
            raise StorageError(f"Failed to create product: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"Failed to create product: {exc}") from exc
        return to_product(item)

    def get_product(self, sku: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.table.get_item(Key={"sku": sku})
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"Failed to read product: {exc}") from exc
        item = (response or {}).get("Item")
        if not item:
            return None
        return to_product(item)

    def list_products(
        self, limit: int = 50, cursor: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": int(limit)}
        if cursor:
            kwargs["ExclusiveStartKey"] = decode_cursor(cursor)
        try:
            response = self.table.scan(**kwargs)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"Failed to list products: {exc}") from exc
        items = [to_product(raw) for raw in (response or {}).get("Items", [])]
        last_key = (response or {}).get("LastEvaluatedKey")
        next_cursor = encode_cursor(last_key) if last_key else None
        return items, next_cursor

    def adjust_stock(self, sku: str, delta: int) -> Dict[str, Any]:
        delta = int(delta)
        values: Dict[str, Any] = {":delta": Decimal(delta), ":now": utc_now()}
        condition = "attribute_exists(#sku)"
        if delta < 0:
            condition += " AND #qty >= :needed"
            values[":needed"] = Decimal(-delta)
        try:
            response = self.table.update_item(
                Key={"sku": sku},
                UpdateExpression="SET #qty = #qty + :delta, #updated = :now",
                ConditionExpression=condition,
                ExpressionAttributeNames={"#sku": "sku", "#qty": "quantity", "#updated": "updated_at"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if _error_code(exc) == CONDITIONAL_FAILED:
                if self.get_product(sku) is None:
                    raise ProductNotFound(f"Product with sku '{sku}' was not found") from exc
                raise InsufficientStock(
                    f"Adjustment of {delta} would drive stock for sku '{sku}' below zero"
                ) from exc
            raise StorageError(f"Failed to adjust stock: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"Failed to adjust stock: {exc}") from exc
        return to_product((response or {}).get("Attributes", {}))


class InMemoryProductRepository(ProductRepository):
    """Dependency-free repository used for local development and tests."""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}

    def ping(self) -> bool:
        return True

    def create_product(self, sku: str, name: str, price: Decimal, quantity: int) -> Dict[str, Any]:
        if sku in self._items:
            raise ProductAlreadyExists(f"Product with sku '{sku}' already exists")
        now = utc_now()
        item = {
            "sku": sku,
            "name": name,
            "price": Decimal(str(price)),
            "quantity": int(quantity),
            "created_at": now,
            "updated_at": now,
        }
        self._items[sku] = item
        return to_product(item)

    def get_product(self, sku: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(sku)
        return to_product(item) if item else None

    def list_products(
        self, limit: int = 50, cursor: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        skus = sorted(self._items)
        start = 0
        if cursor:
            last = decode_cursor(cursor)["sku"]
            start = len([sku for sku in skus if sku <= last])
        page = skus[start:start + int(limit)]
        items = [to_product(self._items[sku]) for sku in page]
        next_cursor = None
        if page and start + int(limit) < len(skus):
            next_cursor = encode_cursor({"sku": page[-1]})
        return items, next_cursor

    def adjust_stock(self, sku: str, delta: int) -> Dict[str, Any]:
        item = self._items.get(sku)
        if item is None:
            raise ProductNotFound(f"Product with sku '{sku}' was not found")
        new_quantity = int(item["quantity"]) + int(delta)
        if new_quantity < 0:
            raise InsufficientStock(
                f"Adjustment of {int(delta)} would drive stock for sku '{sku}' below zero"
            )
        item["quantity"] = new_quantity
        item["updated_at"] = utc_now()
        return to_product(item)
