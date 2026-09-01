"""Persistence layer for the expense tracker API.

The module exposes a small ``ExpenseRepository`` interface plus a DynamoDB
implementation.  Keeping the boto3 details behind the interface lets the HTTP
layer be tested without any AWS or LocalStack dependency.
"""
import base64
import binascii
import json
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key

DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE_NAME = "expenses"
DEFAULT_MONTH_INDEX = "month-date-index"
DEFAULT_CATEGORY_INDEX = "category-date-index"
MAX_SUMMARY_ITEMS = 5000


class StorageError(RuntimeError):
    """Raised when the storage backend cannot service a request."""


class ExpenseNotFoundError(StorageError):
    """Raised when an expense identifier does not exist."""


class InvalidCursorError(StorageError):
    """Raised when a pagination cursor cannot be decoded."""


def _endpoint_url() -> Optional[str]:
    """Return the configured AWS endpoint override (LocalStack friendly)."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def _region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)


def dynamodb_resource():
    """Build a DynamoDB resource honouring AWS_ENDPOINT_URL."""
    return boto3.resource(
        "dynamodb",
        region_name=_region(),
        endpoint_url=_endpoint_url(),
    )


def dynamodb_client():
    """Build a low level DynamoDB client honouring AWS_ENDPOINT_URL."""
    return boto3.client(
        "dynamodb",
        region_name=_region(),
        endpoint_url=_endpoint_url(),
    )


def encode_cursor(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey as an opaque base64 token."""
    if not key:
        return None
    payload = json.dumps(key, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode an opaque pagination token back into a DynamoDB key."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursorError("cursor could not be decoded") from exc
    if not isinstance(decoded, dict):
        raise InvalidCursorError("cursor payload must be an object")
    return decoded


def _error_code(exc: Exception) -> str:
    """Extract an AWS error code from a botocore style exception."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def to_storage(item: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare an item for DynamoDB: drop nulls and convert floats."""
    stored: Dict[str, Any] = {}
    for key, value in item.items():
        if value is None:
            continue
        if isinstance(value, float):
            stored[key] = Decimal(str(value))
        else:
            stored[key] = value
    return stored


class ExpenseRepository:
    """Interface implemented by concrete expense stores."""

    def health(self) -> Dict[str, Any]:
        raise NotImplementedError

    def put(self, item: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def get(self, user_id: str, expense_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def update(self, user_id: str, expense_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def delete(self, user_id: str, expense_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def list_expenses(
        self,
        user_id: str,
        category: Optional[str] = None,
        month: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        raise NotImplementedError

    def iter_month(self, user_id: str, month: str) -> List[Dict[str, Any]]:
        raise NotImplementedError


class DynamoDBExpenseRepository(ExpenseRepository):
    """DynamoDB backed implementation of :class:`ExpenseRepository`."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        month_index: Optional[str] = None,
        category_index: Optional[str] = None,
    ) -> None:
        self.table_name = table_name or os.environ.get("EXPENSES_TABLE", DEFAULT_TABLE_NAME)
        self.month_index = month_index or os.environ.get("EXPENSES_MONTH_INDEX", DEFAULT_MONTH_INDEX)
        self.category_index = category_index or os.environ.get(
            "EXPENSES_CATEGORY_INDEX", DEFAULT_CATEGORY_INDEX
        )
        self._table = None
        self._client = None

    def table(self):
        """Lazily create and cache the DynamoDB table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    def client(self):
        """Lazily create and cache the low level DynamoDB client."""
        if self._client is None:
            self._client = dynamodb_client()
        return self._client

    def health(self) -> Dict[str, Any]:
        description = self.client().describe_table(TableName=self.table_name)
        table = description.get("Table", {}) if isinstance(description, dict) else {}
        return {
            "name": "dynamodb",
            "status": "ok",
            "table": self.table_name,
            "table_status": table.get("TableStatus", "UNKNOWN"),
        }

    def put(self, item: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self.table().put_item(Item=to_storage(item))
        except Exception as exc:
            raise StorageError("failed to write expense: %s" % exc) from exc
        return item

    def get(self, user_id: str, expense_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.table().get_item(Key={"user_id": user_id, "expense_id": expense_id})
        except Exception as exc:
            raise StorageError("failed to read expense: %s" % exc) from exc
        return response.get("Item")

    def update(self, user_id: str, expense_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        prepared = to_storage(changes)
        if not prepared:
            raise StorageError("no fields to update")
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments = []
        for index, field in enumerate(sorted(prepared)):
            names["#f%d" % index] = field
            values[":v%d" % index] = prepared[field]
            assignments.append("#f%d = :v%d" % (index, index))
        try:
            response = self.table().update_item(
                Key={"user_id": user_id, "expense_id": expense_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=Attr("expense_id").exists(),
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if _error_code(exc) == "ConditionalCheckFailedException":
                raise ExpenseNotFoundError(expense_id) from exc
            raise StorageError("failed to update expense: %s" % exc) from exc
        return response.get("Attributes") or {}

    def delete(self, user_id: str, expense_id: str) -> Dict[str, Any]:
        try:
            response = self.table().delete_item(
                Key={"user_id": user_id, "expense_id": expense_id},
                ReturnValues="ALL_OLD",
            )
        except Exception as exc:
            raise StorageError("failed to delete expense: %s" % exc) from exc
        attributes = response.get("Attributes")
        if not attributes:
            raise ExpenseNotFoundError(expense_id)
        return attributes

    def _build_query(
        self,
        user_id: str,
        category: Optional[str],
        month: Optional[str],
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if category:
            condition = Key("category").eq(category)
            if month:
                condition = condition & Key("date").begins_with(month)
            kwargs["IndexName"] = self.category_index
            kwargs["KeyConditionExpression"] = condition
            kwargs["FilterExpression"] = Attr("user_id").eq(user_id)
        elif month:
            kwargs["IndexName"] = self.month_index
            kwargs["KeyConditionExpression"] = Key("month").eq(month)
            kwargs["FilterExpression"] = Attr("user_id").eq(user_id)
        else:
            kwargs["KeyConditionExpression"] = Key("user_id").eq(user_id)
        return kwargs

    def list_expenses(
        self,
        user_id: str,
        category: Optional[str] = None,
        month: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs = self._build_query(user_id, category, month)
        kwargs["Limit"] = int(limit)
        start_key = decode_cursor(cursor)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        try:
            response = self.table().query(**kwargs)
        except Exception as exc:
            raise StorageError("failed to list expenses: %s" % exc) from exc
        items = list(response.get("Items", []))
        return items, encode_cursor(response.get("LastEvaluatedKey"))

    def iter_month(self, user_id: str, month: str) -> List[Dict[str, Any]]:
        kwargs = self._build_query(user_id, None, month)
        items: List[Dict[str, Any]] = []
        while True:
            try:
                response = self.table().query(**kwargs)
            except Exception as exc:
                raise StorageError("failed to summarise expenses: %s" % exc) from exc
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key or len(items) >= MAX_SUMMARY_ITEMS:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items
