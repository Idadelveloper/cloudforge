"""DynamoDB data access layer for the expense tracker API.

This module is the only place that talks to AWS. It is deliberately small so
the API layer can be exercised against an in-memory fake during tests.

Table design (single table, PAY_PER_REQUEST):
    partition key : user_id
    sort key      : sk        -> "<YYYY-MM-DD>#<expense_id>"
    GSI           : gsi1pk    -> "<user_id>#<category>", sort key sk
"""

import base64
import json
import logging
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key

LOGGER = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE_NAME = "expenses"
DEFAULT_CATEGORY_INDEX = "expenses-gsi-category"


def region_name() -> str:
    """AWS region used by every client (defaults to us-east-1)."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def table_name() -> str:
    """Name of the DynamoDB table holding expenses."""
    return os.environ.get("EXPENSES_TABLE", DEFAULT_TABLE_NAME)


def category_index_name() -> str:
    """Name of the category global secondary index."""
    return os.environ.get("EXPENSES_CATEGORY_INDEX", DEFAULT_CATEGORY_INDEX)


def dynamodb_resource() -> Any:
    """Build a DynamoDB resource honouring AWS_ENDPOINT_URL (LocalStack)."""
    return boto3.resource(
        "dynamodb",
        region_name=region_name(),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def encode_cursor(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey as an opaque base64 cursor."""
    if not key:
        return None
    raw = json.dumps(key, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode an opaque cursor back into an ExclusiveStartKey."""
    if not cursor:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid cursor")
    return data


class DynamoDBExpenseRepository:
    """Repository exposing the handful of operations the API needs."""

    def __init__(self, table: Any = None, index_name: Optional[str] = None) -> None:
        self._table = table
        self._index_name = index_name or category_index_name()

    @property
    def table(self) -> Any:
        """Lazily resolve the boto3 Table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(table_name())
        return self._table

    def health(self) -> bool:
        """Return True when the table can be described."""
        try:
            return bool(self.table.table_status)
        except Exception as exc:
            LOGGER.warning("dynamodb health check failed: %s", exc)
            return False

    def put_expense(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Create or replace an expense item."""
        self.table.put_item(Item=item)
        return dict(item)

    def get_expense(self, user_id: str, expense_id: str) -> Optional[Dict[str, Any]]:
        """Find an expense by id within a user's partition."""
        start_key: Optional[Dict[str, Any]] = None
        while True:
            params: Dict[str, Any] = {
                "KeyConditionExpression": Key("user_id").eq(user_id),
                "FilterExpression": Attr("expense_id").eq(expense_id),
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            response = self.table.query(**params)
            for item in response.get("Items", []):
                if item.get("expense_id") == expense_id:
                    return dict(item)
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return None

    def delete_expense(self, user_id: str, sort_key: str) -> None:
        """Delete an expense by its primary key."""
        self.table.delete_item(Key={"user_id": user_id, "sk": sort_key})

    def list_expenses(
        self,
        user_id: str,
        category: Optional[str] = None,
        month: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Query one page of expenses, newest first."""
        params: Dict[str, Any] = {"Limit": limit, "ScanIndexForward": False}
        if category:
            params["IndexName"] = self._index_name
            condition = Key("gsi1pk").eq("%s#%s" % (user_id, category))
        else:
            condition = Key("user_id").eq(user_id)
        if month:
            condition = condition & Key("sk").begins_with(month)
        params["KeyConditionExpression"] = condition
        if cursor:
            params["ExclusiveStartKey"] = cursor
        response = self.table.query(**params)
        items = [dict(item) for item in response.get("Items", [])]
        return items, response.get("LastEvaluatedKey")

    def iter_month_expenses(self, user_id: str, month: str) -> Iterator[Dict[str, Any]]:
        """Yield every expense recorded by a user in the given month."""
        start_key: Optional[Dict[str, Any]] = None
        while True:
            params: Dict[str, Any] = {
                "KeyConditionExpression": Key("user_id").eq(user_id) & Key("sk").begins_with(month),
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            response = self.table.query(**params)
            for item in response.get("Items", []):
                yield dict(item)
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return
