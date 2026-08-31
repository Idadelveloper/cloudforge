"""Offline tests for the todo_api service. No AWS or network access required."""
import pytest
from fastapi.testclient import TestClient

import app as app_module
import storage


@pytest.fixture()
def repo():
    return storage.InMemoryTaskRepository()


@pytest.fixture()
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _create(client, description="write tests", due_date="2030-01-15", completed=None):
    body = {"description": description, "due_date": due_date}
    if completed is not None:
        body["completed"] = completed
    return client.post("/tasks", json=body)


class BrokenRepository(storage.InMemoryTaskRepository):
    """Repository whose backend always fails."""

    def ping(self):
        raise RuntimeError("dynamodb unreachable")

    def list(self, completed=None):
        raise RuntimeError("dynamodb unreachable")


class ConditionalCheckFailedException(Exception):
    """Stand-in for the boto3 conditional check error."""


class FakeClient:
    def __init__(self):
        self.described = []

    def describe_table(self, TableName):
        self.described.append(TableName)
        return {"Table": {"TableStatus": "ACTIVE"}}


class FakeMeta:
    def __init__(self, client):
        self.client = client


class FakeTable:
    def __init__(self):
        self.items = {}
        self.scan_calls = []

    def put_item(self, Item):
        self.items[Item["task_id"]] = dict(Item)
        return {}

    def get_item(self, Key):
        item = self.items.get(Key["task_id"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        return {"Items": [dict(entry) for entry in self.items.values()]}

    def update_item(self, **kwargs):
        task_id = kwargs["Key"]["task_id"]
        item = self.items.get(task_id)
        if item is None:
            raise ConditionalCheckFailedException("ConditionalCheckFailed")
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        for placeholder, attribute in names.items():
            item[attribute] = values[":v" + placeholder[2:]]
        return {"Attributes": dict(item)}

    def delete_item(self, Key, ReturnValues=None):
        item = self.items.pop(Key["task_id"], None)
        return {"Attributes": item} if item else {}


class FakeResource:
    def __init__(self, table):
        self._table = table
        self.meta = FakeMeta(FakeClient())
        self.requested = []

    def Table(self, name):
        self.requested.append(name)
        return self._table


def test_root(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "todo_api"
    assert "POST /tasks" in body["endpoints"]


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dynamodb"] == "available"


def test_health_degraded():
    app_module.app.dependency_overrides[app_module.get_repository] = BrokenRepository
    with TestClient(app_module.app) as test_client:
        body = test_client.get("/health").json()
    app_module.app.dependency_overrides.clear()
    assert body["status"] == "degraded"
    assert body["dynamodb"] == "unavailable"


def test_storage_failure_returns_503():
    app_module.app.dependency_overrides[app_module.get_repository] = BrokenRepository
    with TestClient(app_module.app) as test_client:
        response = test_client.get("/tasks")
    app_module.app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["detail"] == "storage backend unavailable"


def test_create_task(client, repo):
    response = _create(client, description="  buy milk  ", due_date="2030-02-01")
    assert response.status_code == 201
    body = response.json()
    assert body["description"] == "buy milk"
    assert body["due_date"] == "2030-02-01"
    assert body["completed"] is False
    assert body["completed_at"] is None
    assert body["created_at"] and body["updated_at"]
    assert body["task_id"]
    assert repo.get(body["task_id"]) is not None


def test_create_task_completed_flag(client):
    body = _create(client, completed=True).json()
    assert body["completed"] is True
    assert body["completed_at"] is not None


def test_create_task_invalid_due_date(client):
    response = _create(client, due_date="01-15-2030")
    assert response.status_code == 422


def test_create_task_blank_description(client):
    response = _create(client, description="   ")
    assert response.status_code == 422


def test_create_task_missing_field(client):
    response = client.post("/tasks", json={"description": "no due date"})
    assert response.status_code == 422


def test_list_tasks_and_filter(client):
    first = _create(client, description="a", due_date="2030-01-02").json()
    second = _create(client, description="b", due_date="2030-01-01").json()

    response = client.get("/tasks")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [entry["task_id"] for entry in body["items"]] == [second["task_id"], first["task_id"]]

    client.patch("/tasks/{0}/complete".format(first["task_id"]))

    done = client.get("/tasks", params={"completed": "true"}).json()
    assert done["count"] == 1
    assert done["items"][0]["task_id"] == first["task_id"]

    pending = client.get("/tasks", params={"completed": "false"}).json()
    assert pending["count"] == 1
    assert pending["items"][0]["task_id"] == second["task_id"]


def test_get_task(client):
    created = _create(client).json()
    response = client.get("/tasks/{0}".format(created["task_id"]))
    assert response.status_code == 200
    assert response.json() == created


def test_get_task_not_found(client):
    response = client.get("/tasks/does-not-exist")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_complete_task(client):
    created = _create(client).json()
    response = client.patch("/tasks/{0}/complete".format(created["task_id"]))
    assert response.status_code == 200
    body = response.json()
    assert body["completed"] is True
    assert body["completed_at"] is not None


def test_complete_task_not_found(client):
    response = client.patch("/tasks/missing/complete")
    assert response.status_code == 404


def test_update_task_fields(client):
    created = _create(client).json()
    response = client.patch(
        "/tasks/{0}".format(created["task_id"]),
        json={"description": "updated", "due_date": "2031-12-31", "completed": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "updated"
    assert body["due_date"] == "2031-12-31"
    assert body["completed"] is True
    assert body["completed_at"] is not None

    reopened = client.patch("/tasks/{0}".format(created["task_id"]), json={"completed": False}).json()
    assert reopened["completed"] is False
    assert reopened["completed_at"] is None


def test_update_task_empty_body(client):
    created = _create(client).json()
    response = client.patch("/tasks/{0}".format(created["task_id"]), json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "no updatable fields provided"


def test_update_task_invalid_due_date(client):
    created = _create(client).json()
    response = client.patch("/tasks/{0}".format(created["task_id"]), json={"due_date": "nope"})
    assert response.status_code == 422


def test_update_task_not_found(client):
    response = client.patch("/tasks/missing", json={"description": "x"})
    assert response.status_code == 404


def test_delete_task(client):
    created = _create(client).json()
    response = client.delete("/tasks/{0}".format(created["task_id"]))
    assert response.status_code == 200
    assert response.json() == {"task_id": created["task_id"], "deleted": True}
    assert client.delete("/tasks/{0}".format(created["task_id"])).status_code == 404


def test_dynamodb_repository_crud(monkeypatch):
    table = FakeTable()
    resource = FakeResource(table)
    monkeypatch.setattr(storage.boto3, "resource", lambda *args, **kwargs: resource)

    repo = storage.DynamoDBTaskRepository(table="tasks-test")

    item = {
        "task_id": "t1",
        "description": "demo",
        "due_date": "2030-01-01",
        "completed": False,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "completed_at": None,
    }
    assert repo.create(item)["task_id"] == "t1"
    assert repo.get("t1")["description"] == "demo"
    assert repo.get("missing") is None
    assert repo.list() == [item]

    repo.list(completed=True)
    assert "FilterExpression" in table.scan_calls[-1]

    updated = repo.update("t1", {"completed": True, "completed_at": "2024-01-02T00:00:00Z"})
    assert updated["completed"] is True
    assert updated["completed_at"] == "2024-01-02T00:00:00Z"
    assert repo.update("t1", {}) == updated
    assert repo.update("missing", {"completed": True}) is None

    assert repo.ping() is True
    assert resource.meta.client.described == ["tasks-test"]

    assert repo.delete("t1") is True
    assert repo.delete("t1") is False


def test_dynamodb_repository_reraises_other_errors(monkeypatch):
    class ExplodingTable(FakeTable):
        def update_item(self, **kwargs):
            raise RuntimeError("throughput exceeded")

    resource = FakeResource(ExplodingTable())
    monkeypatch.setattr(storage.boto3, "resource", lambda *args, **kwargs: resource)
    repo = storage.DynamoDBTaskRepository(table="tasks-test")
    with pytest.raises(RuntimeError):
        repo.update("t1", {"completed": True})


def test_dynamodb_resource_uses_endpoint_env(monkeypatch):
    captured = {}

    def fake_resource(service_name, **kwargs):
        captured["service_name"] = service_name
        captured.update(kwargs)
        return "resource"

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    assert storage.dynamodb_resource() == "resource"
    assert captured["service_name"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "eu-west-1"


def test_dynamodb_resource_defaults(monkeypatch):
    captured = {}

    def fake_resource(service_name, **kwargs):
        captured.update(kwargs)
        return "resource"

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    storage.dynamodb_resource()
    assert captured["endpoint_url"] is None
    assert captured["region_name"] == "us-east-1"


def test_table_name_env(monkeypatch):
    monkeypatch.delenv("TASKS_TABLE", raising=False)
    assert storage.table_name() == "tasks"
    monkeypatch.setenv("TASKS_TABLE", "my-tasks")
    assert storage.table_name() == "my-tasks"


def test_get_repository_is_lazy_singleton(monkeypatch):
    app_module.set_repository(None)
    created = []

    class Dummy(storage.InMemoryTaskRepository):
        pass

    def factory():
        created.append(1)
        return Dummy()

    monkeypatch.setattr(app_module, "DynamoDBTaskRepository", factory)
    first = app_module.get_repository()
    second = app_module.get_repository()
    assert first is second
    assert len(created) == 1
    app_module.set_repository(None)
