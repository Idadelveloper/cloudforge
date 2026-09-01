"""Data access layer for the shop inventory API.

Defines a tiny repository interface with two implementations:

* :class:`DynamoDBProductRepository` - production implementation using boto3.
* :class:`InMemoryProductRepository` - dependency-free implementation used by
  the test-suite so that tests never touch the network.
"""

import base64
import binascii
import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3

LOGGER = logging.getLogger(__name__)

DEFAULT_TABLE_NAME = "shop-inventory-products"
DEFAULT_REGION = "us-east-1"
CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"


class StorageError(Exception):
    """Base class for storage level failures."""


class ProductNotFound(StorageError):
    """Raised when a SKU does not exist."""

    def __init__(self, sku: str) -> None:
        super().__init__(f"product with sku '{sku}' was not found")
        self.sku = sku


class ProductAlreadyExists(StorageError):
    """Raised when creating a product whose SKU is already taken."""

    def __init__(self, sku: str) -> None:
        super().__init__(f"product with sku '{sku}' already exists")
        self.sku = sku


class InsufficientStock(StorageError):
    """Raised when a decrement would drive quantity below zero."""

    def __init__(self, sku: str, available: int, delta: int) -> None:
        super().__init__(
            f"cannot adjust stock of '{sku}' by {delta}: only {available} unit(s) on hand"
        )
        self.sku = sku
        self.available = available
        self.delta = delta


class InvalidPaginationToken(StorageError):
    """Raised when the caller supplies a malformed next_token."""

    def __init__(self, detail: str = "invalid pagination token") -> None:
        super().__init__(f"invalid pagination token: {detail}")


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def products_table_name() -> str:
    """Table name from the environment (LocalStack / real AWS friendly)."""
    return os.environ.get("PRODUCTS_TABLE", DEFAULT_TABLE_NAME)


def aws_region() -> str:
    """Region from the environment, defaulting to us-east-1."""
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )


def dynamodb_resource():
    """Create a DynamoDB resource honouring AWS_ENDPOINT_URL when present."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def _error_code(exc: Exception) -> str:
    """Extract the AWS error code from a botocore ClientError-like exception."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def _encode_token(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_token(token: str) -> Dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise InvalidPaginationToken(str(exc)) from exc
    if not isinstance(data, dict):
        raise InvalidPaginationToken("token must decode to an object")
    return data


def _normalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert DynamoDB Decimals into JSON friendly Python numbers."""
    out: Dict[str, Any] = dict(item)
    if "price" in out and out["price"] is not None:
        out["price"] = float(out["price"])
    if "quantity" in out and out["quantity"] is not None:
        out["quantity"] = int(out["quantity"])
    return out


class ProductRepository(ABC):
    """Interface used by the API layer."""

    table_name: str = DEFAULT_TABLE_NAME

    @abstractmethod
    def create(self, product: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a new product or raise :class:`ProductAlreadyExists`."""

    @abstractmethod
    def get(self, sku: str) -> Optional[Dict[str, Any]]:
        """Return a product or None."""

    @abstractmethod
    def list_products(
        self, limit: int = 50, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of products plus an optional next token."""

    @abstractmethod
    def update_attributes(
        self, sku: str, name: Optional[str] = None, price: Optional[float] = None
    ) -> Dict[str, Any]:
        """Update name/price of an existing product."""

    @abstractmethod
    def adjust_stock(self, sku: str, delta: int) -> Dict[str, Any]:
        """Apply a signed delta to quantity, never going below zero."""

    @abstractmethod
    def healthy(self) -> bool:
        """Return True when the backing store is reachable."""


class DynamoDBProductRepository(ProductRepository):
    """Repository backed by a single DynamoDB table keyed by ``sku``."""

    def __init__(self, table_name: Optional[str] = None, table: Any = None) -> None:
        self.table_name = table_name or products_table_name()
        self._table_obj = table

    def _table(self):
        if self._table_obj is None:
            self._table_obj = dynamodb_resource().Table(self.table_name)
        return self._table_obj

    def create(self, product: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(product)
        item["price"] = Decimal(str(item["price"]))
        item["quantity"] = int(item["quantity"])
        try:
            self._table().put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(sku)",
            )
        except Exception as exc:
            if _error_code(exc) == CONDITIONAL_CHECK_FAILED:
                raise ProductAlreadyExists(str(item["sku"])) from exc
            raise
        return _normalize_item(item)

    def get(self, sku: str) -> Optional[Dict[str, Any]]:
        response = self._table().get_item(Key={"sku": sku})
        item = response.get("Item")
        if not item:
            return None
        return _normalize_item(item)

    def list_products(
        self, limit: int = 50, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": int(limit)}
        if next_token:
            kwargs["ExclusiveStartKey"] = _decode_token(next_token)
        response = self._table().scan(**kwargs)
        items = [_normalize_item(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        token = _encode_token(last_key) if last_key else None
        return items, token

    def update_attributes(
        self, sku: str, name: Optional[str] = None, price: Optional[float] = None
    ) -> Dict[str, Any]:
        names: Dict[str, str] = {"#u": "updated_at"}
        values: Dict[str, Any] = {":u": utc_now_iso()}
        assignments = ["#u = :u"]
        if name is not None:
            names["#n"] = "name"
            values[":n"] = name
            assignments.append("#n = :n")
        if price is not None:
            names["#p"] = "price"
            values[":p"] = Decimal(str(price))
            assignments.append("#p = :p")
        try:
            response = self._table().update_item(
                Key={"sku": sku},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(sku)",
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if _error_code(exc) == CONDITIONAL_CHECK_FAILED:
                raise ProductNotFound(sku) from exc
            raise
        return _normalize_item(response.get("Attributes") or {})

    def adjust_stock(self, sku: str, delta: int) -> Dict[str, Any]:
        delta = int(delta)
        names = {"#q": "quantity", "#u": "updated_at"}
        values: Dict[str, Any] = {":d": Decimal(delta), ":u": utc_now_iso()}
        condition = "attribute_exists(sku)"
        if delta < 0:
            values[":needed"] = Decimal(-delta)
            condition += " AND #q >= :needed"
        try:
            response = self._table().update_item(
                Key={"sku": sku},
                UpdateExpression="SET #u = :u ADD #q :d",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=condition,
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if _error_code(exc) == CONDITIONAL_CHECK_FAILED:
                existing = self.get(sku)
                if existing is None:
                    raise ProductNotFound(sku) from exc
                available = int(existing.get("quantity", 0) or 0)
                raise InsufficientStock(sku, available, delta) from exc
            raise
        return _normalize_item(response.get("Attributes") or {})

    def healthy(self) -> bool:
        try:
            self._table().load()
            return True
        except Exception as exc:  # noqa: BLE001 - health check must not raise
            LOGGER.warning("DynamoDB table %s unreachable: %s", self.table_name, exc)
            return False


class InMemoryProductRepository(ProductRepository):
    """Thread-safe in-memory repository (tests / local experiments)."""

    def __init__(self, table_name: str = "in-memory-products") -> None:
        self.table_name = table_name
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, product: Dict[str, Any]) -> Dict[str, Any]:
        sku = str(product["sku"])
        with self._lock:
            if sku in self._items:
                raise ProductAlreadyExists(sku)
            stored = _normalize_item(product)
            self._items[sku] = stored
        return dict(stored)

    def get(self, sku: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._items.get(sku)
            return dict(item) if item else None

    def list_products(
        self, limit: int = 50, next_token: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        start = 0
        if next_token:
            data = _decode_token(next_token)
            try:
                start = int(data.get("offset", 0))
            except (TypeError, ValueError) as exc:
                raise InvalidPaginationToken("offset must be an integer") from exc
            if start < 0:
                raise InvalidPaginationToken("offset must not be negative")
        with self._lock:
            items = [dict(item) for item in self._items.values()]
        page = items[start:start + int(limit)]
        end = start + int(limit)
        token = _encode_token({"offset": end}) if end < len(items) else None
        return page, token

    def update_attributes(
        self, sku: str, name: Optional[str] = None, price: Optional[float] = None
    ) -> Dict[str, Any]:
        with self._lock:
            item = self._items.get(sku)
            if item is None:
                raise ProductNotFound(sku)
            if name is not None:
                item["name"] = name
            if price is not None:
                item["price"] = float(price)
            item["updated_at"] = utc_now_iso()
            return dict(item)

    def adjust_stock(self, sku: str, delta: int) -> Dict[str, Any]:
        delta = int(delta)
        with self._lock:
            item = self._items.get(sku)
            if item is None:
                raise ProductNotFound(sku)
            current = int(item.get("quantity", 0) or 0)
            new_quantity = current + delta
            if new_quantity < 0:
                raise InsufficientStock(sku, current, delta)
            item["quantity"] = new_quantity
            item["updated_at"] = utc_now_iso()
            return dict(item)

    def healthy(self) -> bool:
        return True
