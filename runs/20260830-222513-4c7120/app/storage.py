"""Data access layer for the todo_api service.

A tiny repository interface hides boto3 so the HTTP layer can be exercised with
an in-memory implementation during tests.
"""
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr

DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE_NAME = "tasks"


def region_name() -> str:
    """Resolve the AWS region from the environment."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def table_name() -> str:
    """Resolve the DynamoDB table name from the environment."""
    return os.environ.get("TASKS_TABLE", DEFAULT_TABLE_NAME)


def dynamodb_resource() -> Any:
    """Build a DynamoDB resource honouring AWS_ENDPOINT_URL (LocalStack)."""
    return boto3.resource(
        "dynamodb",
        region_name=region_name(),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def _is_conditional_check_failure(exc: Exception) -> bool:
    """Detect a DynamoDB ConditionalCheckFailedException without importing botocore."""
    if type(exc).__name__ == "ConditionalCheckFailedException":
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict) and error.get("Code") == "ConditionalCheckFailedException":
            return True
    return "ConditionalCheckFailed" in str(exc)


class TaskRepository(ABC):
    """Persistence contract used by the API layer."""

    @abstractmethod
    def ping(self) -> bool:
        """Return True when the backing store is reachable."""

    @abstractmethod
    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a new task item and return it."""

    @abstractmethod
    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return a task item or None when missing."""

    @abstractmethod
    def list(self, completed: Optional[bool] = None) -> List[Dict[str, Any]]:
        """Return all task items, optionally filtered on completion state."""

    @abstractmethod
    def update(self, task_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply attribute changes; return updated item or None when missing."""

    @abstractmethod
    def delete(self, task_id: str) -> bool:
        """Delete a task; return True when something was removed."""


class DynamoDBTaskRepository(TaskRepository):
    """DynamoDB-backed repository."""

    def __init__(self, table: Optional[str] = None, resource: Any = None) -> None:
        self._table_name = table or table_name()
        self._resource = resource if resource is not None else dynamodb_resource()

    @property
    def table(self) -> Any:
        return self._resource.Table(self._table_name)

    def ping(self) -> bool:
        client = self._resource.meta.client
        client.describe_table(TableName=self._table_name)
        return True

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        self.table.put_item(Item=item)
        return dict(item)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key={"task_id": task_id})
        item = response.get("Item")
        return dict(item) if item else None

    def list(self, completed: Optional[bool] = None) -> List[Dict[str, Any]]:
        scan_kwargs: Dict[str, Any] = {}
        if completed is not None:
            scan_kwargs["FilterExpression"] = Attr("completed").eq(bool(completed))
        items: List[Dict[str, Any]] = []
        table = self.table
        while True:
            response = table.scan(**scan_kwargs)
            items.extend(dict(entry) for entry in response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
        return items

    def update(self, task_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not changes:
            return self.get(task_id)
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments: List[str] = []
        for index, (attribute, value) in enumerate(changes.items()):
            name_key = "#f{0}".format(index)
            value_key = ":v{0}".format(index)
            names[name_key] = attribute
            values[value_key] = value
            assignments.append("{0} = {1}".format(name_key, value_key))
        try:
            response = self.table.update_item(
                Key={"task_id": task_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(task_id)",
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if _is_conditional_check_failure(exc):
                return None
            raise
        attributes = response.get("Attributes")
        return dict(attributes) if attributes else None

    def delete(self, task_id: str) -> bool:
        response = self.table.delete_item(Key={"task_id": task_id}, ReturnValues="ALL_OLD")
        return bool(response.get("Attributes"))


class InMemoryTaskRepository(TaskRepository):
    """Dict-backed repository used for tests and local experimentation."""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}

    def ping(self) -> bool:
        return True

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        self._items[item["task_id"]] = dict(item)
        return dict(item)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(task_id)
        return dict(item) if item else None

    def list(self, completed: Optional[bool] = None) -> List[Dict[str, Any]]:
        items = [dict(entry) for entry in self._items.values()]
        if completed is not None:
            items = [entry for entry in items if bool(entry.get("completed")) is bool(completed)]
        return items

    def update(self, task_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        item = self._items.get(task_id)
        if item is None:
            return None
        item.update(changes)
        return dict(item)

    def delete(self, task_id: str) -> bool:
        return self._items.pop(task_id, None) is not None
