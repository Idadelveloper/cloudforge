"""Data access layer: DynamoDB persistence for shop products.

The application only depends on the small ``ProductRepository`` interface, so tests
can inject a fake DynamoDB resource (or a fake repository) and run fully offline.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3

DEFAULT_TABLE_NAME = "products"
DEFAULT_REGION = "us-east-1"
DEFAULT_LOG_GROUP = "/shop-inventory-api/application"
HEALTH_PROBE_SKU = "__health_probe__"

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Base class for domain level storage errors."""


class ProductNotFoundError(StorageError):
    """Raised when a SKU does not exist."""

    def __init__(self, sku: str) -> None:
        super().__init__("product '{0}' was not found".format(sku))
        self.sku = sku


class ProductExistsError(StorageError):
    """Raised when creating a product whose SKU is already used."""

    def __init__(self, sku: str) -> None:
        super().__init__("product '{0}' already exists".format(sku))
        self.sku = sku


class InsufficientStockError(StorageError):
    """Raised when an adjustment would drive the quantity below zero."""

    def __init__(self, sku: str) -> None:
        super().__init__("adjustment would drive stock for '{0}' below zero".format(sku))
        self.sku = sku


class InvalidPaginationTokenError(StorageError):
    """Raised when a supplied next_token cursor cannot be decoded."""

    def __init__(self) -> None:
        super().__init__("next_token is not a valid pagination cursor")


def products_table_name() -> str:
    """Name of the DynamoDB products table."""
    return os.environ.get("PRODUCTS_TABLE", DEFAULT_TABLE_NAME)


def application_log_group() -> str:
    """CloudWatch log group the application writes structured logs to."""
    return os.environ.get("APPLICATION_LOG_GROUP", DEFAULT_LOG_GROUP)


def configure_logging() -> None:
    """Configure a single structured stdout handler (shipped to CloudWatch)."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                '{"time": "%(asctime)s", "level": "%(levelname)s", '
                '"logger": "%(name)s", "message": "%(message)s"}'
            )
        )
        root.addHandler(handler)
    root.setLevel(level)
    logger.debug("logging configured, target log group %s", application_log_group())


def dynamodb_resource():
    """Create a DynamoDB resource honouring AWS_ENDPOINT_URL (LocalStack)."""
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _aws_error_code(exc: Exception) -> str:
    """Extract the AWS error code from a botocore ClientError-like exception."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def encode_token(last_key: Dict[str, Any]) -> str:
    """Encode a DynamoDB LastEvaluatedKey into an opaque cursor."""
    raw = json.dumps(last_key, default=str, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def decode_token(token: str) -> Dict[str, Any]:
    """Decode an opaque cursor back into a DynamoDB ExclusiveStartKey."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidPaginationTokenError() from exc
    if not isinstance(data, dict) or "sku" not in data:
        raise InvalidPaginationTokenError()
    return {"sku": str(data["sku"])}


def deserialize_item(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalise a raw DynamoDB item into plain Python types."""
    item = item or {}
    return {
        "sku": str(item.get("sku", "")),
        "name": str(item.get("name", "")),
        "price": Decimal(str(item.get("price", "0"))),
        "quantity": int(item.get("quantity", 0)),
        "created_at": str(item.get("created_at", "")),
        "updated_at": str(item.get("updated_at", "")),
    }


class ProductRepository:
    """Interface implemented by concrete product repositories."""

    def create_product(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def get_product(self, sku: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def list_products(
        self,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        raise NotImplementedError

    def adjust_stock(self, sku: str, delta: int) -> Dict[str, Any]:
        raise NotImplementedError

    def healthy(self) -> bool:
        raise NotImplementedError


class DynamoDBProductRepository(ProductRepository):
    """Product repository backed by a single DynamoDB table keyed by ``sku``."""

    def __init__(self, table_name: Optional[str] = None, resource: Any = None) -> None:
        self.table_name = table_name or products_table_name()
        self._resource = resource if resource is not None else dynamodb_resource()
        self._table = self._resource.Table(self.table_name)

    # -- writes ---------------------------------------------------------
    def create_product(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = _utc_now()
        sku = str(payload["sku"])
        item = {
            "sku": sku,
            "name": str(payload["name"]),
            "price": Decimal(str(payload.get("price", "0"))),
            "quantity": Decimal(int(payload.get("quantity") or 0)),
            "created_at": now,
            "updated_at": now,
        }
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(sku)")
        except Exception as exc:
            if _aws_error_code(exc) == "ConditionalCheckFailedException":
                raise ProductExistsError(sku) from exc
            logger.error("put_item failed for sku=%s: %s", sku, exc)
            raise
        return deserialize_item(item)

    def adjust_stock(self, sku: str, delta: int) -> Dict[str, Any]:
        delta = int(delta)
        now = _utc_now()
        condition = "attribute_exists(sku)"
        values: Dict[str, Any] = {":delta": Decimal(delta), ":now": now}
        if delta < 0:
            condition = condition + " AND quantity >= :min_quantity"
            values[":min_quantity"] = Decimal(-delta)
        try:
            result = self._table.update_item(
                Key={"sku": sku},
                UpdateExpression="SET quantity = quantity + :delta, updated_at = :now",
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if _aws_error_code(exc) == "ConditionalCheckFailedException":
                if self.get_product(sku) is None:
                    raise ProductNotFoundError(sku) from exc
                raise InsufficientStockError(sku) from exc
            logger.error("update_item failed for sku=%s: %s", sku, exc)
            raise
        return deserialize_item(result.get("Attributes", {}))

    # -- reads ----------------------------------------------------------
    def get_product(self, sku: str) -> Optional[Dict[str, Any]]:
        response = self._table.get_item(Key={"sku": sku})
        item = response.get("Item")
        if not item:
            return None
        return deserialize_item(item)

    def list_products(
        self,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": int(limit)}
        if next_token:
            kwargs["ExclusiveStartKey"] = decode_token(next_token)
        response = self._table.scan(**kwargs)
        items = [deserialize_item(raw) for raw in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        token = encode_token(last_key) if last_key else None
        return items, token

    def healthy(self) -> bool:
        """Cheap read against the table; requires only dynamodb:GetItem."""
        try:
            self._table.get_item(Key={"sku": HEALTH_PROBE_SKU})
            return True
        except Exception as exc:  # noqa: BLE001 - health probes never raise
            logger.warning("dynamodb health probe failed: %s", exc)
            return False
