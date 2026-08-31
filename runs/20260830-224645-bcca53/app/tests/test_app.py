"""Offline tests for the to-do task API.

Every AWS interaction is stubbed: the HTTP tests inject an in-memory
repository, and the DynamoDB repository is exercised against a fake table
object that mimics the boto3 ``Table`` resource API.
"""

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402
from app import app, get_repository  # noqa: E402
from storage import DynamoTaskRepository, InMemoryTaskRepository  # noqa: E402


class FailingHealthRepository(InMemoryTaskRepository):
    """Repository whose datastore check always fails."""

    def healthy(self) -> bool:
        return False


class FakeClientError(Exception):
    """Minimal stand-in for botocore's ClientError."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class StubTable:
    """Fake boto3 DynamoDB Table resource."""

    def __init__(self):
        self.items = {}
        self.scan_calls = []
        self.describe_calls = []
        self.fail_describe = False
        self.update_error = None
        self.delete_error = None

    @property
    def meta(self):
        client = SimpleNamespace(describe_table=self._describe_table)
        return SimpleNamespace(client=client)

    def _describe_table(self, **kwargs):
        self.describe_calls.append(kwargs)
        if self.fail_describe:
            raise FakeClientError("ResourceNotFoundException")
        return {"Table": {"TableName": kwargs.get("TableName")}}

    def put_item(self, **kwargs):
        item = dict(kwargs["Item"])
        self.items[item["task_id"]] = item
        return {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]["task_id"]
        item = self.items.get(key)
        if item is None:
            return {}
        return {"Item": dict(item)}

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        return {"Items": [dict(item) for item in self.items.values()]}

    def update_item(self, **kwargs):
        if self.update_error is not None:
            raise FakeClientError(self.update_error)
        key = kwargs["Key"]["task_id"]
        if key not in self.items:
            raise FakeClientError("ConditionalCheckFailedException")
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        item = self.items[key]
        for name_key, attribute in names.items():
            value_key = ":v" + name_key[2:]
            item[attribute] = values[value_key]
        return {"Attributes": dict(item)}

    def delete_item(self, **kwargs):
        if self.delete_error is not None:
            raise FakeClientError(self.delete_error)
        key = kwargs["Key"]["task_id"]
        if key not in self.items:
            raise FakeClientError("ConditionalCheckFailedException")
        del self.items[key]
        return {}


@pytest.fixture
def repository():
    return InMemoryTaskRepository()


@pytest.fixture
def client(repository):
    app.dependency_overrides[get_repository] = lambda: repository
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _create(client, description="Buy milk", due_date="2030-01-31", **extra):
    payload = {"description": description, "due_date": due_date}
    payload.update(extra)
    response = client.post("/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["table"]


def test_health_unavailable():
    app.dependency_overrides[get_repository] = lambda: FailingHealthRepository()
    with TestClient(app) as test_client:
        response = test_client.get("/health")
    app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["detail"] == "datastore unavailable"


def test_create_task(client):
    task = _create(client, description="  Walk the dog  ", due_date="2030-02-01")
    assert task["task_id"]
    assert task["description"] == "Walk the dog"
    assert task["due_date"] == "2030-02-01"
    assert task["completed"] is False
    assert task["completed_at"] is None
    assert task["created_at"]
    assert task["updated_at"]


def test_create_task_completed_flag(client):
    task = _create(client, completed=True)
    assert task["completed"] is True
    assert task["completed_at"]


def test_create_task_validation_errors(client):
    assert client.post("/tasks", json={"description": "  ", "due_date": "2030-01-01"}).status_code == 422
    assert client.post("/tasks", json={"description": "x", "due_date": "31-01-2030"}).status_code == 422
    assert client.post("/tasks", json={"due_date": "2030-01-01"}).status_code == 422


def test_list_tasks_and_filter(client):
    first = _create(client, description="a", due_date="2030-01-01")
    _create(client, description="b", due_date="2030-06-01")

    response = client.get("/tasks")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [item["description"] for item in body["items"]] == ["a", "b"]

    client.post("/tasks/{0}/complete".format(first["task_id"]))

    done = client.get("/tasks", params={"completed": "true"}).json()
    assert done["count"] == 1
    assert done["items"][0]["description"] == "a"

    pending = client.get("/tasks", params={"completed": "false"}).json()
    assert pending["count"] == 1
    assert pending["items"][0]["description"] == "b"


def test_list_tasks_limit(client):
    _create(client, description="a", due_date="2030-01-01")
    _create(client, description="b", due_date="2030-02-01")
    body = client.get("/tasks", params={"limit": 1}).json()
    assert body["count"] == 1


def test_get_task(client):
    task = _create(client)
    response = client.get("/tasks/{0}".format(task["task_id"]))
    assert response.status_code == 200
    assert response.json()["task_id"] == task["task_id"]


def test_get_task_missing(client):
    response = client.get("/tasks/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


def test_patch_task(client):
    task = _create(client)
    response = client.patch(
        "/tasks/{0}".format(task["task_id"]),
        json={"description": "New text", "due_date": "2031-12-25", "completed": True},
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["description"] == "New text"
    assert updated["due_date"] == "2031-12-25"
    assert updated["completed"] is True
    assert updated["completed_at"]

    reopened = client.patch("/tasks/{0}".format(task["task_id"]), json={"completed": False}).json()
    assert reopened["completed"] is False
    assert reopened["completed_at"] is None


def test_patch_task_errors(client):
    task = _create(client)
    assert client.patch("/tasks/{0}".format(task["task_id"]), json={}).status_code == 400
    bad_date = client.patch("/tasks/{0}".format(task["task_id"]), json={"due_date": "nope"})
    assert bad_date.status_code == 422
    empty = client.patch("/tasks/{0}".format(task["task_id"]), json={"description": ""})
    assert empty.status_code == 422
    assert client.patch("/tasks/missing", json={"completed": True}).status_code == 404


def test_complete_task(client):
    task = _create(client)
    response = client.post("/tasks/{0}/complete".format(task["task_id"]))
    assert response.status_code == 200
    body = response.json()
    assert body["completed"] is True
    assert body["completed_at"]


def test_complete_task_missing(client):
    assert client.post("/tasks/missing/complete").status_code == 404


def test_delete_task(client):
    task = _create(client)
    response = client.delete("/tasks/{0}".format(task["task_id"]))
    assert response.status_code == 204
    assert client.delete("/tasks/{0}".format(task["task_id"])).status_code == 404
    assert client.get("/tasks").json()["count"] == 0


def test_dynamodb_resource_uses_environment(monkeypatch):
    captured = {}

    def fake_resource(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return "resource"

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    assert storage.dynamodb_resource() == "resource"
    assert captured["service"] == "dynamodb"
    assert captured["region_name"] == "us-east-1"
    assert captured["endpoint_url"] == "http://localhost:4566"


def test_dynamodb_resource_without_endpoint(monkeypatch):
    captured = {}

    def fake_resource(service, **kwargs):
        captured.update(kwargs)
        return "resource"

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")

    storage.dynamodb_resource()
    assert captured["endpoint_url"] is None
    assert captured["region_name"] == "eu-west-1"


def test_table_name_from_environment(monkeypatch):
    monkeypatch.delenv("TASKS_TABLE_NAME", raising=False)
    monkeypatch.delenv("TASKS_TABLE", raising=False)
    assert storage.table_name() == "todo_tasks"
    monkeypatch.setenv("TASKS_TABLE_NAME", "other_tasks")
    assert storage.table_name() == "other_tasks"


def test_dynamo_repository_crud():
    table = StubTable()
    repo = DynamoTaskRepository(name="todo_tasks", table=table)

    item = {
        "task_id": "t-1",
        "description": "write tests",
        "due_date": "2030-03-03",
        "completed": False,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "completed_at": None,
    }
    created = repo.create(item)
    assert created["task_id"] == "t-1"

    fetched = repo.get("t-1")
    assert fetched is not None
    assert fetched["description"] == "write tests"
    assert repo.get("missing") is None

    listed = repo.list_tasks()
    assert len(listed) == 1

    filtered = repo.list_tasks(completed=True)
    assert "FilterExpression" in table.scan_calls[-1]
    assert isinstance(filtered, list)

    updated = repo.update("t-1", {"completed": True, "completed_at": "2024-01-02T00:00:00Z"})
    assert updated is not None
    assert updated["completed"] is True
    assert updated["completed_at"] == "2024-01-02T00:00:00Z"

    assert repo.update("missing", {"completed": True}) is None
    assert repo.update("t-1", {}) is not None

    assert repo.healthy() is True
    table.fail_describe = True
    assert repo.healthy() is False

    assert repo.delete("t-1") is True
    assert repo.delete("t-1") is False


def test_dynamo_repository_propagates_unexpected_errors():
    table = StubTable()
    repo = DynamoTaskRepository(name="todo_tasks", table=table)
    table.items["t-1"] = {"task_id": "t-1"}

    table.update_error = "ProvisionedThroughputExceededException"
    with pytest.raises(FakeClientError):
        repo.update("t-1", {"completed": True})

    table.delete_error = "InternalServerError"
    with pytest.raises(FakeClientError):
        repo.delete("t-1")


def test_dynamo_repository_builds_table_from_resource():
    table = StubTable()
    resource = SimpleNamespace(Table=lambda name: table)
    repo = DynamoTaskRepository(name="my_tasks", resource=resource)
    assert repo.table is table
    assert repo.table is table
