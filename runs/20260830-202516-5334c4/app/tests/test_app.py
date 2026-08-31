"""Offline tests for the personal notes API (no AWS/network access)."""
import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import lambda_handler as lambda_module
import storage


@pytest.fixture()
def repo():
    """Fresh in-memory repository wired into the app dependency."""
    repository = storage.InMemoryNotesRepository()
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repository
    yield repository
    app_module.app.dependency_overrides.clear()


@pytest.fixture()
def client(repo):
    with TestClient(app_module.app) as test_client:
        yield test_client


def _create(client, title="Shopping list", body="milk, eggs"):
    response = client.post("/notes", json={"title": title, "body": body})
    assert response.status_code == 201, response.text
    return response.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_note(client):
    note = _create(client)
    assert note["note_id"]
    assert note["owner_id"] == "default-user"
    assert note["title"] == "Shopping list"
    assert note["body"] == "milk, eggs"
    assert note["created_at"] == note["updated_at"]


def test_create_note_validation_error(client):
    response = client.post("/notes", json={"title": "", "body": "x"})
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "validation_error"
    assert "title" in payload["detail"]


def test_create_note_missing_body_field(client):
    response = client.post("/notes", json={"title": "only title"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_get_note(client):
    note = _create(client)
    response = client.get("/notes/{0}".format(note["note_id"]))
    assert response.status_code == 200
    assert response.json() == note


def test_get_note_not_found(client):
    response = client.get("/notes/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "Note not found", "code": "not_found"}


def test_list_notes_pagination(client):
    created = [_create(client, title="note-{0}".format(index)) for index in range(3)]
    ids = {note["note_id"] for note in created}

    first = client.get("/notes", params={"limit": 2})
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["count"] == 2
    assert len(first_payload["items"]) == 2
    assert first_payload["next_token"]

    second = client.get("/notes", params={"limit": 2, "next_token": first_payload["next_token"]})
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["count"] == 1
    assert second_payload["next_token"] is None

    seen = {item["note_id"] for item in first_payload["items"] + second_payload["items"]}
    assert seen == ids


def test_list_notes_empty(client):
    response = client.get("/notes")
    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0, "next_token": None}


def test_list_notes_invalid_token(client):
    response = client.get("/notes", params={"next_token": "!!!not-a-token!!!"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid next_token", "code": "bad_request"}


def test_list_notes_invalid_limit(client):
    response = client.get("/notes", params={"limit": 0})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_update_note(client):
    note = _create(client)
    response = client.put(
        "/notes/{0}".format(note["note_id"]),
        json={"title": "new title", "body": "new body"},
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["title"] == "new title"
    assert updated["body"] == "new body"
    assert updated["created_at"] == note["created_at"]

    fetched = client.get("/notes/{0}".format(note["note_id"])).json()
    assert fetched["title"] == "new title"


def test_update_note_not_found(client):
    response = client.put("/notes/missing", json={"title": "t", "body": "b"})
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_delete_note(client):
    note = _create(client)
    response = client.delete("/notes/{0}".format(note["note_id"]))
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/notes/{0}".format(note["note_id"])).status_code == 404


def test_delete_note_not_found(client):
    response = client.delete("/notes/missing")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


class BrokenRepository(storage.NotesRepository):
    """Repository that always fails, used to check the 503 mapping."""

    def list_notes(self, owner_id, limit, next_token=None):
        raise storage.StorageError("dynamodb exploded")


def test_storage_error_returns_503():
    app_module.app.dependency_overrides[app_module.get_repository] = BrokenRepository
    try:
        with TestClient(app_module.app) as test_client:
            response = test_client.get("/notes")
        assert response.status_code == 503
        assert response.json()["code"] == "storage_unavailable"
    finally:
        app_module.app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Lambda adapter
# --------------------------------------------------------------------------- #
def test_lambda_handler_health(repo):
    response = lambda_module.lambda_handler({"httpMethod": "GET", "path": "/health"}, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["status"] == "ok"


def test_lambda_handler_create_and_list(repo):
    create_event = {
        "httpMethod": "POST",
        "path": "/notes",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"title": "from lambda", "body": "hello"}),
    }
    created = lambda_module.lambda_handler(create_event, None)
    assert created["statusCode"] == 201
    note = json.loads(created["body"])
    assert note["title"] == "from lambda"

    list_event = {
        "httpMethod": "GET",
        "path": "/notes",
        "queryStringParameters": {"limit": "10"},
    }
    listed = lambda_module.lambda_handler(list_event, None)
    assert listed["statusCode"] == 200
    assert json.loads(listed["body"])["count"] == 1


# --------------------------------------------------------------------------- #
# DynamoDB repository (boto3 fully stubbed)
# --------------------------------------------------------------------------- #
class FakeTable:
    """Very small stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self):
        self.rows = {}
        self.last_evaluated_key = None
        self.query_error = None

    @staticmethod
    def _conditional_error(operation):
        return storage.ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "missing"}},
            operation,
        )

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.rows[(item["owner_id"], item["note_id"])] = dict(item)
        return {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        row = self.rows.get((key["owner_id"], key["note_id"]))
        return {"Item": dict(row)} if row else {}

    def query(self, **kwargs):
        if self.query_error is not None:
            raise self.query_error
        limit = kwargs.get("Limit", 25)
        items = [dict(row) for row in self.rows.values()][:limit]
        response = {"Items": items}
        if self.last_evaluated_key:
            response["LastEvaluatedKey"] = self.last_evaluated_key
        return response

    def update_item(self, **kwargs):
        key = kwargs["Key"]
        row_key = (key["owner_id"], key["note_id"])
        if row_key not in self.rows:
            raise self._conditional_error("UpdateItem")
        values = kwargs["ExpressionAttributeValues"]
        row = self.rows[row_key]
        row["title"] = values[":title"]
        row["body"] = values[":body"]
        row["updated_at"] = values[":updated_at"]
        return {"Attributes": dict(row)}

    def delete_item(self, **kwargs):
        key = kwargs["Key"]
        row_key = (key["owner_id"], key["note_id"])
        if row_key not in self.rows:
            raise self._conditional_error("DeleteItem")
        del self.rows[row_key]
        return {}


class FakeResource:
    """Stand-in for ``boto3.resource('dynamodb')``."""

    def __init__(self, table):
        self._table = table
        self.requested = []

    def Table(self, name):  # noqa: N802 - mirrors the boto3 API
        self.requested.append(name)
        return self._table


@pytest.fixture()
def fake_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(storage.boto3, "resource", lambda *args, **kwargs: FakeResource(table))
    return table


def _item(note_id="n1", title="t", body="b"):
    return {
        "owner_id": "default-user",
        "note_id": note_id,
        "title": title,
        "body": body,
        "created_at": "2024-01-01T00:00:00.000Z",
        "updated_at": "2024-01-01T00:00:00.000Z",
    }


def test_dynamodb_repository_crud(fake_table):
    repository = storage.DynamoDBNotesRepository("notes-table")
    repository.put_note(_item())

    assert repository.get_note("default-user", "n1")["title"] == "t"
    assert repository.get_note("default-user", "nope") is None

    items, token = repository.list_notes("default-user", 10)
    assert len(items) == 1
    assert token is None

    updated = repository.update_note("default-user", "n1", "new", "body", "2024-02-02T00:00:00.000Z")
    assert updated["title"] == "new"
    assert updated["updated_at"] == "2024-02-02T00:00:00.000Z"

    assert repository.delete_note("default-user", "n1") is True
    assert repository.delete_note("default-user", "n1") is False
    assert repository.update_note("default-user", "n1", "x", "y", "z") is None


def test_dynamodb_repository_pagination_token(fake_table):
    repository = storage.DynamoDBNotesRepository("notes-table")
    repository.put_note(_item("n1"))
    fake_table.last_evaluated_key = {"owner_id": "default-user", "note_id": "n1"}

    _items, token = repository.list_notes("default-user", 1)
    assert token
    assert storage.decode_token(token) == {"owner_id": "default-user", "note_id": "n1"}

    # The token round-trips as an ExclusiveStartKey without raising.
    again, _next_token = repository.list_notes("default-user", 1, token)
    assert len(again) == 1


def test_dynamodb_repository_client_error(fake_table):
    repository = storage.DynamoDBNotesRepository("notes-table")
    fake_table.query_error = storage.ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}},
        "Query",
    )
    with pytest.raises(storage.StorageError):
        repository.list_notes("default-user", 10)


def test_token_helpers():
    assert storage.encode_token(None) is None
    assert storage.decode_token(None) is None
    token = storage.encode_token({"offset": 5})
    assert storage.decode_token(token) == {"offset": 5}
    with pytest.raises(storage.InvalidTokenError):
        storage.decode_token("@@@@")


def test_build_repository_selection(monkeypatch):
    monkeypatch.setenv("NOTES_BACKEND", "memory")
    assert isinstance(storage.build_repository(), storage.InMemoryNotesRepository)
    monkeypatch.setenv("NOTES_BACKEND", "dynamodb")
    assert isinstance(storage.build_repository(), storage.DynamoDBNotesRepository)


def test_get_repository_is_cached(monkeypatch):
    monkeypatch.setenv("NOTES_BACKEND", "memory")
    app_module.set_repository(None)
    first = app_module.get_repository()
    assert app_module.get_repository() is first
    app_module.set_repository(None)


def test_dynamodb_resource_uses_endpoint_url(monkeypatch):
    captured = {}

    def fake_resource(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    storage.dynamodb_resource()
    assert captured["service"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "us-east-1"
