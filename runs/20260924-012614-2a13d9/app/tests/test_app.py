"""Offline tests for the personal notes API. No AWS access is performed."""

import os
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage  # noqa: E402
from storage import (  # noqa: E402
    DynamoDBNotesRepository,
    InMemoryNotesRepository,
    InvalidCursorError,
    NoteNotFoundError,
    decode_cursor,
    encode_cursor,
)


class FakeTable:
    """Minimal stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, healthy=True):
        self.items = {}
        self._healthy = healthy
        self.meta = SimpleNamespace(client=SimpleNamespace(describe_table=self._describe_table))

    def _describe_table(self, **kwargs):
        if not self._healthy:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
                "DescribeTable",
            )
        return {"Table": {"TableName": kwargs.get("TableName"), "TableStatus": "ACTIVE"}}

    def put_item(self, **kwargs):
        item = dict(kwargs["Item"])
        self.items[item["note_id"]] = item
        return {}

    def get_item(self, **kwargs):
        note_id = kwargs["Key"]["note_id"]
        if note_id in self.items:
            return {"Item": dict(self.items[note_id])}
        return {}

    def scan(self, **kwargs):
        ordered = list(self.items.values())
        start = 0
        start_key = kwargs.get("ExclusiveStartKey")
        if start_key:
            ids = [entry["note_id"] for entry in ordered]
            if start_key["note_id"] in ids:
                start = ids.index(start_key["note_id"]) + 1
        limit = kwargs.get("Limit", len(ordered))
        page = ordered[start:start + limit]
        response = {"Items": [dict(entry) for entry in page]}
        if page and (start + limit) < len(ordered):
            response["LastEvaluatedKey"] = {"note_id": page[-1]["note_id"]}
        return response

    def update_item(self, **kwargs):
        note_id = kwargs["Key"]["note_id"]
        if note_id not in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "missing"}},
                "UpdateItem",
            )
        item = self.items[note_id]
        for placeholder, value in kwargs["ExpressionAttributeValues"].items():
            item[placeholder.lstrip(":")] = value
        return {"Attributes": dict(item)}

    def delete_item(self, **kwargs):
        note_id = kwargs["Key"]["note_id"]
        if note_id not in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "missing"}},
                "DeleteItem",
            )
        del self.items[note_id]
        return {}


@pytest.fixture()
def repo():
    return InMemoryNotesRepository()


@pytest.fixture()
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _create(client, title="first", body="hello"):
    response = client.post("/notes", json={"title": title, "body": body})
    assert response.status_code == 201, response.text
    return response.json()


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["dynamodb"] == "reachable"
    assert payload["table"]


def test_health_degraded(client, repo):
    repo.available = False
    payload = client.get("/health").json()
    assert payload["status"] == "degraded"
    assert payload["dynamodb"] == "unreachable"


def test_create_note(client):
    note = _create(client, title="shopping", body="milk")
    assert note["title"] == "shopping"
    assert note["body"] == "milk"
    assert note["note_id"]
    assert note["created_at"] == note["updated_at"]


def test_create_note_defaults_body(client):
    response = client.post("/notes", json={"title": "only-title"})
    assert response.status_code == 201
    assert response.json()["body"] == ""


def test_create_note_validation_error(client):
    response = client.post("/notes", json={"title": ""})
    assert response.status_code == 422


def test_list_notes_and_pagination(client):
    for index in range(3):
        _create(client, title="note-%d" % index)
    first = client.get("/notes", params={"limit": 2}).json()
    assert first["count"] == 2
    assert len(first["items"]) == 2
    assert first["next_cursor"]

    second = client.get("/notes", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert second["count"] == 1
    assert second["next_cursor"] is None


def test_list_notes_invalid_cursor(client):
    response = client.get("/notes", params={"cursor": "not-a-cursor"})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_list_notes_limit_out_of_range(client):
    assert client.get("/notes", params={"limit": 0}).status_code == 422


def test_get_note(client):
    note = _create(client)
    response = client.get("/notes/%s" % note["note_id"])
    assert response.status_code == 200
    assert response.json() == note


def test_get_note_missing(client):
    response = client.get("/notes/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert "not found" in body["detail"]


def test_update_note(client):
    note = _create(client, title="old", body="body")
    response = client.put("/notes/%s" % note["note_id"], json={"title": "new"})
    assert response.status_code == 200
    updated = response.json()
    assert updated["title"] == "new"
    assert updated["body"] == "body"
    assert updated["created_at"] == note["created_at"]


def test_update_note_requires_field(client):
    note = _create(client)
    response = client.put("/notes/%s" % note["note_id"], json={})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_update_note_missing(client):
    response = client.put("/notes/missing", json={"body": "x"})
    assert response.status_code == 404


def test_delete_note(client):
    note = _create(client)
    response = client.delete("/notes/%s" % note["note_id"])
    assert response.status_code == 204
    assert client.get("/notes/%s" % note["note_id"]).status_code == 404


def test_delete_note_missing(client):
    response = client.delete("/notes/missing")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_cursor_roundtrip():
    assert encode_cursor(None) is None
    assert decode_cursor(None) is None
    cursor = encode_cursor({"note_id": "abc"})
    assert decode_cursor(cursor) == {"note_id": "abc"}
    with pytest.raises(InvalidCursorError):
        decode_cursor("%%%not-base64%%%")


def test_dynamodb_repository_crud():
    table = FakeTable()
    repository = DynamoDBNotesRepository(table=table, name="notes")

    created = repository.create("title", "body")
    assert created["note_id"] in table.items

    fetched = repository.get(created["note_id"])
    assert fetched["title"] == "title"

    updated = repository.update(created["note_id"], title="changed", body="new body")
    assert updated["title"] == "changed"
    assert updated["body"] == "new body"

    repository.create("second", "")
    items, cursor = repository.list_notes(limit=1)
    assert len(items) == 1
    assert cursor
    more, next_cursor = repository.list_notes(limit=1, cursor=cursor)
    assert len(more) == 1
    assert next_cursor is None

    repository.delete(created["note_id"])
    assert created["note_id"] not in table.items


def test_dynamodb_repository_missing_items():
    repository = DynamoDBNotesRepository(table=FakeTable(), name="notes")
    with pytest.raises(NoteNotFoundError):
        repository.get("nope")
    with pytest.raises(NoteNotFoundError):
        repository.update("nope", title="x")
    with pytest.raises(NoteNotFoundError):
        repository.delete("nope")


def test_dynamodb_repository_health():
    assert DynamoDBNotesRepository(table=FakeTable(), name="notes").healthy() is True
    assert DynamoDBNotesRepository(table=FakeTable(healthy=False), name="notes").healthy() is False


def test_dynamodb_resource_uses_endpoint_and_region(monkeypatch):
    captured = {}

    def fake_resource(service_name, **kwargs):
        captured["service"] = service_name
        captured.update(kwargs)
        return SimpleNamespace(Table=lambda name: SimpleNamespace(table_name=name))

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")

    resource = storage.dynamodb_resource()
    assert captured["service"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "eu-west-1"
    assert resource.Table("notes").table_name == "notes"


def test_table_name_default(monkeypatch):
    monkeypatch.delenv("NOTES_TABLE_NAME", raising=False)
    assert storage.table_name() == "notes"
    monkeypatch.setenv("NOTES_TABLE_NAME", "custom-notes")
    assert storage.table_name() == "custom-notes"


def test_get_repository_is_lazy_singleton(monkeypatch):
    monkeypatch.setattr(app_module, "_repository", None)
    first = app_module.get_repository()
    second = app_module.get_repository()
    assert isinstance(first, DynamoDBNotesRepository)
    assert first is second
    monkeypatch.setattr(app_module, "_repository", None)
