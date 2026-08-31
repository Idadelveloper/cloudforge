"""Offline tests for the personal notes API.

The HTTP layer is exercised with an in-memory repository injected via FastAPI's
dependency overrides; the DynamoDB repository is exercised against a fake boto3
table.  No network or LocalStack access is required.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402
from app import app, get_repository  # noqa: E402
from storage import (  # noqa: E402
    DynamoNotesRepository,
    InvalidTokenError,
    NoteNotFound,
    decode_token,
    encode_token,
)


class FakeNotesRepository:
    """In-memory stand-in for :class:`DynamoNotesRepository`."""

    def __init__(self):
        self.table_name = "notes"
        self.items = {}
        self.reachable = True

    def health(self):
        if not self.reachable:
            return False, "table missing"
        return True, "ACTIVE"

    def create(self, note):
        self.items[note["id"]] = dict(note)
        return dict(note)

    def get(self, note_id):
        item = self.items.get(note_id)
        return dict(item) if item else None

    def list_notes(self, limit=50, next_token=None):
        keys = sorted(self.items)
        start = 0
        if next_token:
            if not next_token.isdigit():
                raise InvalidTokenError("Invalid next_token")
            start = int(next_token)
        page = keys[start:start + limit]
        end = start + limit
        token = str(end) if end < len(keys) else None
        return [dict(self.items[key]) for key in page], token

    def update(self, note_id, fields, updated_at):
        if note_id not in self.items:
            raise NoteNotFound(note_id)
        item = self.items[note_id]
        item.update(fields)
        item["updated_at"] = updated_at
        return dict(item)

    def delete(self, note_id):
        if note_id not in self.items:
            raise NoteNotFound(note_id)
        del self.items[note_id]


@pytest.fixture()
def repo():
    fake = FakeNotesRepository()
    app.dependency_overrides[get_repository] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture()
def client(repo):
    with TestClient(app) as test_client:
        yield test_client


def _create(client, title="Shopping", body="milk"):
    response = client.post("/notes", json={"title": title, "body": body})
    assert response.status_code == 201, response.text
    return response.json()


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["dynamodb"] == "available"
    assert payload["table"] == "notes"


def test_health_reports_unavailable_table(client, repo):
    repo.reachable = False
    payload = client.get("/health").json()
    assert payload["dynamodb"] == "unavailable"
    assert payload["detail"] == "table missing"


def test_create_note(client):
    note = _create(client)
    assert note["title"] == "Shopping"
    assert note["body"] == "milk"
    assert note["id"]
    assert note["created_at"] == note["updated_at"]


def test_create_note_defaults_body(client):
    response = client.post("/notes", json={"title": "only title"})
    assert response.status_code == 201
    assert response.json()["body"] == ""


def test_create_note_validation_error(client):
    response = client.post("/notes", json={"body": "no title"})
    assert response.status_code == 422
    assert "detail" in response.json()

    response = client.post("/notes", json={"title": ""})
    assert response.status_code == 422


def test_list_notes_and_pagination(client):
    for index in range(3):
        _create(client, title="note-%d" % index)

    response = client.get("/notes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 3
    assert payload["next_token"] is None
    assert len(payload["items"]) == 3

    first = client.get("/notes", params={"limit": 2}).json()
    assert first["count"] == 2
    assert first["next_token"] == "2"

    second = client.get("/notes", params={"limit": 2, "next_token": first["next_token"]}).json()
    assert second["count"] == 1
    assert second["next_token"] is None


def test_list_notes_invalid_token(client):
    response = client.get("/notes", params={"next_token": "not-a-cursor"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid next_token"


def test_list_notes_invalid_limit(client):
    assert client.get("/notes", params={"limit": 0}).status_code == 422
    assert client.get("/notes", params={"limit": 1000}).status_code == 422


def test_get_note(client):
    note = _create(client)
    response = client.get("/notes/%s" % note["id"])
    assert response.status_code == 200
    assert response.json() == note


def test_get_note_missing(client):
    response = client.get("/notes/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "Note not found"


def test_update_note(client):
    note = _create(client)
    response = client.put("/notes/%s" % note["id"], json={"title": "Groceries"})
    assert response.status_code == 200
    updated = response.json()
    assert updated["title"] == "Groceries"
    assert updated["body"] == note["body"]
    assert updated["created_at"] == note["created_at"]


def test_update_note_both_fields(client):
    note = _create(client)
    response = client.put("/notes/%s" % note["id"], json={"title": "T", "body": "B"})
    assert response.status_code == 200
    assert response.json()["title"] == "T"
    assert response.json()["body"] == "B"


def test_update_note_requires_fields(client):
    note = _create(client)
    response = client.put("/notes/%s" % note["id"], json={})
    assert response.status_code == 400


def test_update_note_missing(client):
    response = client.put("/notes/nope", json={"title": "x"})
    assert response.status_code == 404


def test_delete_note(client):
    note = _create(client)
    response = client.delete("/notes/%s" % note["id"])
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/notes/%s" % note["id"]).status_code == 404


def test_delete_note_missing(client):
    response = client.delete("/notes/nope")
    assert response.status_code == 404


def test_token_roundtrip():
    token = encode_token({"id": "abc"})
    assert decode_token(token) == {"id": "abc"}
    assert encode_token(None) is None
    with pytest.raises(InvalidTokenError):
        decode_token("@@@not-base64@@@")
    with pytest.raises(InvalidTokenError):
        decode_token(encode_token({"id": "x"})[:3])


# --------------------------------------------------------------------------
# DynamoDB repository tests against a fake boto3 table
# --------------------------------------------------------------------------


class ConditionalCheckFailedException(Exception):
    """Mimics the boto3 modelled DynamoDB exception."""


class FakeClientExceptions:
    ConditionalCheckFailedException = ConditionalCheckFailedException


class FakeClient:
    def __init__(self, store):
        self.store = store
        self.exceptions = FakeClientExceptions()
        self.fail_describe = False

    def describe_table(self, TableName):  # noqa: N803 - boto3 style kwarg
        if self.fail_describe:
            raise RuntimeError("table %s not found" % TableName)
        return {"Table": {"TableName": TableName, "TableStatus": "ACTIVE"}}


class FakeMeta:
    def __init__(self, client):
        self.client = client


class FakeTable:
    def __init__(self, name):
        self.name = name
        self.store = {}
        self.client = FakeClient(self.store)
        self.meta = FakeMeta(self.client)

    def put_item(self, Item):  # noqa: N803
        self.store[Item["id"]] = dict(Item)
        return {}

    def get_item(self, Key):  # noqa: N803
        item = self.store.get(Key["id"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        keys = sorted(self.store)
        start_key = kwargs.get("ExclusiveStartKey")
        if start_key:
            keys = [k for k in keys if k > start_key["id"]]
        limit = kwargs.get("Limit", len(keys))
        page = keys[:limit]
        response = {"Items": [dict(self.store[k]) for k in page]}
        if len(keys) > limit and page:
            response["LastEvaluatedKey"] = {"id": page[-1]}
        return response

    def update_item(self, **kwargs):
        note_id = kwargs["Key"]["id"]
        if note_id not in self.store:
            raise ConditionalCheckFailedException("missing")
        names = kwargs["ExpressionAttributeNames"]
        item = self.store[note_id]
        for placeholder, value in kwargs["ExpressionAttributeValues"].items():
            item[names["#n" + placeholder[2:]]] = value
        return {"Attributes": dict(item)}

    def delete_item(self, **kwargs):
        note_id = kwargs["Key"]["id"]
        if note_id not in self.store:
            raise ConditionalCheckFailedException("missing")
        del self.store[note_id]
        return {}


class FakeResource:
    def __init__(self, table):
        self._table = table

    def Table(self, name):  # noqa: N802 - boto3 style method name
        return self._table


@pytest.fixture()
def dynamo_repo(monkeypatch):
    table = FakeTable("notes")
    monkeypatch.setattr(storage, "dynamodb_resource", lambda: FakeResource(table))
    repository = DynamoNotesRepository("notes")
    return repository, table


def test_dynamo_repository_crud(dynamo_repo):
    repository, table = dynamo_repo
    note = repository.create(
        {
            "id": "n1",
            "title": "t",
            "body": "b",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }
    )
    assert note["id"] == "n1"
    assert repository.get("n1")["title"] == "t"
    assert repository.get("missing") is None

    updated = repository.update("n1", {"title": "t2"}, "2024-01-02T00:00:00Z")
    assert updated["title"] == "t2"
    assert updated["updated_at"] == "2024-01-02T00:00:00Z"
    assert updated["created_at"] == "2024-01-01T00:00:00Z"

    with pytest.raises(NoteNotFound):
        repository.update("nope", {"title": "x"}, "2024-01-02T00:00:00Z")

    ok, detail = repository.health()
    assert ok is True
    assert detail == "ACTIVE"

    table.client.fail_describe = True
    ok, detail = repository.health()
    assert ok is False
    assert "not found" in detail

    repository.delete("n1")
    assert repository.get("n1") is None
    with pytest.raises(NoteNotFound):
        repository.delete("n1")


def test_dynamo_repository_list_pagination(dynamo_repo):
    repository, _ = dynamo_repo
    for index in range(3):
        repository.create(
            {
                "id": "id-%d" % index,
                "title": "t%d" % index,
                "body": "",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        )
    items, token = repository.list_notes(limit=2)
    assert len(items) == 2
    assert token is not None

    items, token = repository.list_notes(limit=2, next_token=token)
    assert len(items) == 1
    assert token is None


def test_table_name_env(monkeypatch):
    monkeypatch.setenv("NOTES_TABLE_NAME", "custom-notes")
    assert storage.table_name() == "custom-notes"
    monkeypatch.delenv("NOTES_TABLE_NAME")
    monkeypatch.delenv("NOTES_TABLE", raising=False)
    assert storage.table_name() == "notes"
