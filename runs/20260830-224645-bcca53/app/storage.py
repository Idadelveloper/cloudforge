"""Data access layer for the to-do task API.

The HTTP layer depends only on the small :class:`TaskRepository` interface, so
tests can substitute :class:`InMemoryTaskRepository` and run fully offline.
The DynamoDB implementation honours ``AWS_ENDPOINT_URL`` for LocalStack.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr

DEFAULT_TABLE_NAME = "todo_tasks"
DEFAULT_REGION = "us-east-1"
DEFAULT_LIMIT = 100
MAX_SCAN_PAGES = 20
CONDITION_FAILED = "ConditionalCheckFailedException"


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def new_task_id() -> str:
    """Return a freshly generated task id."""
    return str(uuid.uuid4())


def table_name() -> str:
    """Return the configured DynamoDB table name."""
    return os.environ.get("TASKS_TABLE_NAME") or os.environ.get("TASKS_TABLE") or DEFAULT_TABLE_NAME


def dynamodb_resource():
    """Return a boto3 DynamoDB resource configured from the environment."""
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def _error_code(exc: Exception) -> str:
    """Extract the AWS error code from a botocore style exception."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return ""


def _normalise(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Coerce a raw DynamoDB item into the canonical task shape."""
    if not item:
        return None
    result: Dict[str, Any] = dict(item)
    result["task_id"] = str(result.get("task_id", ""))
    result["description"] = str(result.get("description", ""))
    result["due_date"] = str(result.get("due_date", ""))
    result["completed"] = bool(result.get("completed", False))
    result["created_at"] = str(result.get("created_at", ""))
    result["updated_at"] = str(result.get("updated_at", result.get("created_at", "")))
    completed_at = result.get("completed_at")
    result["completed_at"] = completed_at if isinstance(completed_at, str) else None
    return result


def _sorted(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the tasks ordered by due date then creation time."""
    return sorted(
        items,
        key=lambda item: (
            item.get("due_date") or "",
            item.get("created_at") or "",
            item.get("task_id") or "",
        ),
    )


class TaskRepository:
    """Interface implemented by every task storage backend."""

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def list_tasks(
        self,
        completed: Optional[bool] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def update(self, task_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def delete(self, task_id: str) -> bool:
        raise NotImplementedError

    def healthy(self) -> bool:
        raise NotImplementedError


class DynamoTaskRepository(TaskRepository):
    """DynamoDB backed repository."""

    def __init__(self, name: Optional[str] = None, resource: Any = None, table: Any = None) -> None:
        self.table_name = name or table_name()
        self._resource = resource
        self._table = table

    @property
    def table(self) -> Any:
        if self._table is None:
            resource = self._resource or dynamodb_resource()
            self._table = resource.Table(self.table_name)
        return self._table

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        self.table.put_item(Item=dict(item), ConditionExpression=Attr("task_id").not_exists())
        normalised = _normalise(dict(item))
        return normalised or {}

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key={"task_id": task_id})
        return _normalise(response.get("Item"))

    def list_tasks(
        self,
        completed: Optional[bool] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {}
        if completed is not None:
            kwargs["FilterExpression"] = Attr("completed").eq(bool(completed))

        items: List[Dict[str, Any]] = []
        pages = 0
        while pages < MAX_SCAN_PAGES:
            response = self.table.scan(**kwargs)
            for raw in response.get("Items", []):
                normalised = _normalise(raw)
                if normalised:
                    items.append(normalised)
            pages += 1
            last_key = response.get("LastEvaluatedKey")
            if not last_key or len(items) >= limit:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return _sorted(items)[:limit]

    def update(self, task_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not changes:
            return self.get(task_id)

        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments: List[str] = []
        for index, key in enumerate(sorted(changes)):
            name_key = "#n{0}".format(index)
            value_key = ":v{0}".format(index)
            names[name_key] = key
            values[value_key] = changes[key]
            assignments.append("{0} = {1}".format(name_key, value_key))

        try:
            response = self.table.update_item(
                Key={"task_id": task_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=Attr("task_id").exists(),
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if _error_code(exc) == CONDITION_FAILED:
                return None
            raise
        return _normalise(response.get("Attributes"))

    def delete(self, task_id: str) -> bool:
        try:
            self.table.delete_item(
                Key={"task_id": task_id},
                ConditionExpression=Attr("task_id").exists(),
            )
        except Exception as exc:
            if _error_code(exc) == CONDITION_FAILED:
                return False
            raise
        return True

    def healthy(self) -> bool:
        try:
            self.table.meta.client.describe_table(TableName=self.table_name)
        except Exception:
            return False
        return True


class InMemoryTaskRepository(TaskRepository):
    """In-process repository used for tests and local experimentation."""

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        for item in items or []:
            self._items[str(item["task_id"])] = dict(item)

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        stored = dict(item)
        self._items[str(stored["task_id"])] = stored
        normalised = _normalise(dict(stored))
        return normalised or {}

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(task_id)
        if item is None:
            return None
        return _normalise(dict(item))

    def list_tasks(
        self,
        completed: Optional[bool] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for item in self._items.values():
            if completed is not None and bool(item.get("completed")) != bool(completed):
                continue
            normalised = _normalise(dict(item))
            if normalised:
                results.append(normalised)
        return _sorted(results)[:limit]

    def update(self, task_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        item = self._items.get(task_id)
        if item is None:
            return None
        item.update(changes)
        return _normalise(dict(item))

    def delete(self, task_id: str) -> bool:
        return self._items.pop(task_id, None) is not None

    def healthy(self) -> bool:
        return True
