"""Offline tests for the personal notes API (no AWS or network required)."""

import os
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402
from app import app, get_repository  # noqa: E402
from storage import (  # noqa: E402
    DynamoDBNotesRepository,
    InMemoryNotesRepository,
    InvalidCursorError,
    NoteNotFoundError,
    NotesRepository,
    StorageError,
    decode_cursor,
    encode_cursor,
)


def _client_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, operation)


class FakeDynamoClient:
    """Minimal stand-in for the low level DynamoDB client."""

    def __init__(self, describe_error=None):
        self.describe_error = describe_error
        self.described = []

    def describe_table(self, **kwargs):
        if self.describe_error is not None:
            raise self.describe_error
        self.described.append(kwargs.get("TableName"))
        return {"Table": {"TableStatus": "ACTIVE"}}


class FakeTable:
    """In-process fake of a boto3 DynamoDB Table resource."""

    def __init__(self, describe_error=None, put_error=None):
        self.items = {}
        self.put_error = put_error
        self.meta = SimpleNamespace(client=FakeDynamoClient(describe_error))

    def put_item(self, **kwargs):
        if self.put_error is not None:
            raise self.put_error
        item = dict(kwargs["Item"])
        self.items[item["note_id"]] = item
        return {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]["note_id"]
        if key not in self.items:
            return {}
        return {"Item": dict(self.items[key])}

    def scan(self, **kwargs):
        limit = int(kwargs.get("Limit", 50))
        keys = sorted(self.items)
        start = kwargs.get("ExclusiveStartKey")
        if start:
            keys = [key for key in keys if key > start["note_id"]]
        page = keys[:limit]
        result = {"Items": [dict(self.items[key]) for key in page]}
        if page and len(keys) > limit:
            result["LastEvaluatedKey"] = {"note_id": page[-1]}
        return result

    def update_item(self, **kwargs):
        key = kwargs["Key"]["note_id"]
        if key not in self.items:
            raise _client_error("ConditionalCheckFailedException", "UpdateItem")
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        item = self.items[key]
        for placeholder, field in names.items():
            item[field] = values[":v" + placeholder[2:]]
        return {"Attributes": dict(item)}

    def delete_item(self, **kwargs):
        key = kwargs["Key"]["note_id"]
        if key not in self.items:
            raise _client_error("ConditionalCheckFailedException", "DeleteItem")
        del self.items[key]
        return {}


class BrokenRepository(NotesRepository):
    """Repository that always fails, to exercise the 500 path."""

    def create(self, item):
        raise StorageError("dynamodb down")

    def get(self, note_id):
        raise StorageError("dynamodb down")

    def list(self, limit=50, cursor=None):
        raise StorageError("dynamodb down")

    def update(self, note_id, updates):
        raise StorageError("dynamodb down")

    def delete(self, note_id):
        raise StorageError("dynamodb down")

    def healthy(self):
        return False


@pytest.fixture()
def repo():
    return InMemoryNotesRepository()


@pytest.fixture()
def client(repo):
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _create(client, title="Groceries", body="milk"):
    response = client.post("/notes", json={"title": title, "body": body})
    assert response.status_code == 201
    return response.json()


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["table"]


def test_health_unavailable(client, repo):
    repo.healthy_flag = False
    response = client.get("/health")
    assert response.status_code == 503


def test_create_note(client):
    note = _create(client, "Shopping", "eggs")
    assert note["title"] == "Shopping"
    assert note["body"] == "eggs"
    assert note["note_id"]
    assert note["created_at"].endswith("Z")
    assert note["created_at"] == note["updated_at"]


def test_create_note_defaults_body(client):
    response = client.post("/notes", json={"title": "Only title"})
    assert response.status_code == 201
    assert response.json()["body"] == ""


def test_create_note_validation_errors(client):
    assert client.post("/notes", json={"title": ""}).status_code == 422
    assert client.post("/notes", json={"body": "no title"}).status_code == 422
    extra = client.post("/notes", json={"title": "x", "colour": "red"})
    assert extra.status_code == 422


def test_get_note(client):
    note = _create(client)
    response = client.get("/notes/{0}".format(note["note_id"]))
    assert response.status_code == 200
    assert response.json() == note


def test_get_note_missing(client):
    response = client.get("/notes/does-not-exist")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_list_notes_and_pagination(client):
    created = [_create(client, "note-{0}".format(index)) for index in range(3)]
    assert len(created) == 3

    first = client.get("/notes", params={"limit": 2})
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["count"] == 2
    assert len(first_payload["items"]) == 2
    assert first_payload["next_cursor"]

    second = client.get("/notes", params={"limit": 2, "cursor": first_payload["next_cursor"]})
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["count"] == 1
    assert second_payload["next_cursor"] is None

    seen = {item["note_id"] for item in first_payload["items"] + second_payload["items"]}
    assert seen == {item["note_id"] for item in created}


def test_list_notes_empty(client):
    response = client.get("/notes")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None, "count": 0}


def test_list_notes_invalid_cursor(client):
    response = client.get("/notes", params={"cursor": "!!!not-base64!!!"})
    assert response.status_code == 400
    assert "cursor" in response.json()["detail"]


def test_list_notes_invalid_limit(client):
    assert client.get("/notes", params={"limit": 0}).status_code == 422
    assert client.get("/notes", params={"limit": 5000}).status_code == 422


def test_update_note(client):
    note = _create(client, "Old", "old body")
    response = client.put("/notes/{0}".format(note["note_id"]), json={"title": "New"})
    assert response.status_code == 200
    updated = response.json()
    assert updated["title"] == "New"
    assert updated["body"] == "old body"
    assert updated["created_at"] == note["created_at"]


def test_update_note_body_only(client):
    note = _create(client, "Keep", "before")
    response = client.put("/notes/{0}".format(note["note_id"]), json={"body": "after"})
    assert response.status_code == 200
    assert response.json()["body"] == "after"
    assert response.json()["title"] == "Keep"


def test_update_note_missing(client):
    response = client.put("/notes/nope", json={"title": "x"})
    assert response.status_code == 404


def test_update_note_requires_a_field(client):
    note = _create(client)
    response = client.put("/notes/{0}".format(note["note_id"]), json={})
    assert response.status_code == 422


def test_update_note_rejects_unknown_field(client):
    note = _create(client)
    response = client.put("/notes/{0}".format(note["note_id"]), json={"title": "a", "tag": "b"})
    assert response.status_code == 422


def test_delete_note(client):
    note = _create(client)
    response = client.delete("/notes/{0}".format(note["note_id"]))
    assert response.status_code == 204
    assert client.get("/notes/{0}".format(note["note_id"])).status_code == 404


def test_delete_note_missing(client):
    assert client.delete("/notes/ghost").status_code == 404


def test_storage_failure_returns_500():
    app.dependency_overrides[get_repository] = BrokenRepository
    with TestClient(app) as broken_client:
        assert broken_client.post("/notes", json={"title": "x"}).status_code == 500
        assert broken_client.get("/notes").status_code == 500
        assert broken_client.get("/notes/abc").status_code == 500
        assert broken_client.put("/notes/abc", json={"title": "y"}).status_code == 500
        assert broken_client.delete("/notes/abc").status_code == 500
        assert broken_client.get("/health").status_code == 503
    app.dependency_overrides.clear()


def test_cursor_roundtrip():
    cursor = encode_cursor({"note_id": "abc"})
    assert decode_cursor(cursor) == {"note_id": "abc"}
    assert encode_cursor(None) is None
    assert decode_cursor(None) is None


def test_cursor_rejects_garbage():
    with pytest.raises(InvalidCursorError):
        decode_cursor("@@@")
    with pytest.raises(InvalidCursorError):
        decode_cursor(encode_cursor({"other": "key"}))


def test_dynamodb_repository_crud_with_fake_table():
    table = FakeTable()
    repository = DynamoDBNotesRepository(table=table, table_name="notes-test")

    created = repository.create(
        {
            "note_id": "id-1",
            "title": "t",
            "body": "b",
            "created_at": "2024-01-01T00:00:00.000Z",
            "updated_at": "2024-01-01T00:00:00.000Z",
        }
    )
    assert created["note_id"] == "id-1"
    assert repository.get("id-1")["title"] == "t"

    updated = repository.update("id-1", {"title": "t2", "updated_at": "2024-01-02T00:00:00.000Z"})
    assert updated["title"] == "t2"
    assert updated["updated_at"] == "2024-01-02T00:00:00.000Z"

    repository.create({"note_id": "id-2", "title": "x", "body": "", "created_at": "a", "updated_at": "a"})
    items, cursor = repository.list(limit=1)
    assert len(items) == 1
    assert cursor is not None
    items, cursor = repository.list(limit=1, cursor=cursor)
    assert len(items) == 1
    assert cursor is None

    repository.delete("id-1")
    with pytest.raises(NoteNotFoundError):
        repository.get("id-1")
    assert repository.healthy() is True
    assert table.meta.client.described == ["notes-test"]


def test_dynamodb_repository_missing_item_errors():
    repository = DynamoDBNotesRepository(table=FakeTable(), table_name="notes-test")
    with pytest.raises(NoteNotFoundError):
        repository.update("missing", {"title": "x"})
    with pytest.raises(NoteNotFoundError):
        repository.delete("missing")
    with pytest.raises(ValueError):
        repository.update("missing", {})


def test_dynamodb_repository_wraps_client_errors():
    error = _client_error("ProvisionedThroughputExceededException", "PutItem")
    repository = DynamoDBNotesRepository(table=FakeTable(put_error=error), table_name="notes-test")
    with pytest.raises(StorageError):
        repository.create({"note_id": "x", "title": "t", "body": "", "created_at": "a", "updated_at": "a"})


def test_dynamodb_repository_unhealthy_when_describe_fails():
    error = _client_error("ResourceNotFoundException", "DescribeTable")
    repository = DynamoDBNotesRepository(table=FakeTable(describe_error=error), table_name="notes-test")
    assert repository.healthy() is False


def test_dynamodb_resource_uses_endpoint_and_region(monkeypatch):
    captured = {}

    def fake_resource(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return SimpleNamespace(Table=lambda name: SimpleNamespace(name=name))

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    storage.dynamodb_resource()
    assert captured["service"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "us-east-1"

    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    storage.dynamodb_resource()
    assert captured["endpoint_url"] is None


def test_table_name_from_environment(monkeypatch):
    monkeypatch.setenv("NOTES_TABLE_NAME", "my-notes")
    assert storage.notes_table_name() == "my-notes"
    monkeypatch.delenv("NOTES_TABLE_NAME", raising=False)
    assert storage.notes_table_name() == "notes"


def test_repository_interface_is_abstract():
    base = NotesRepository()
    for call in (
        lambda: base.create({}),
        lambda: base.get("x"),
        lambda: base.list(),
        lambda: base.update("x", {}),
        lambda: base.delete("x"),
        lambda: base.healthy(),
    ):
        with pytest.raises(NotImplementedError):
            call()
