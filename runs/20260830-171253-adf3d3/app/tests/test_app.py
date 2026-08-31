"""Offline tests for the personal notes API and its storage layer."""

import pytest
from fastapi.testclient import TestClient

import app as app_module
import storage
from storage import (
    DynamoDBNotesRepository,
    InMemoryNotesRepository,
    InvalidPageTokenError,
    NoteNotFoundError,
    decode_token,
    encode_token,
)


class ConditionalCheckFailedException(Exception):
    """Stand-in for the boto3 conditional check failure exception."""


class FakeTable:
    """Minimal in-process stand-in for a boto3 DynamoDB Table."""

    def __init__(self):
        self.items = {}
        self.queries = []

    @staticmethod
    def _key(key):
        return (key["user_id"], key["note_id"])

    def put_item(self, **kwargs):
        item = dict(kwargs["Item"])
        self.items[(item["user_id"], item["note_id"])] = item
        return {}

    def get_item(self, **kwargs):
        item = self.items.get(self._key(kwargs["Key"]))
        if item is None:
            return {}
        return {"Item": dict(item)}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        items = sorted(self.items.values(), key=lambda note: note["note_id"])
        start = kwargs.get("ExclusiveStartKey")
        if start:
            items = [note for note in items if note["note_id"] > start["note_id"]]
        limit = int(kwargs.get("Limit", len(items)))
        page = items[:limit]
        response = {"Items": [dict(note) for note in page]}
        if page and len(items) > limit:
            last = page[-1]
            response["LastEvaluatedKey"] = {
                "user_id": last["user_id"],
                "note_id": last["note_id"],
            }
        return response

    def update_item(self, **kwargs):
        key = self._key(kwargs["Key"])
        if key not in self.items:
            raise ConditionalCheckFailedException("condition failed")
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        item = self.items[key]
        for placeholder, field in names.items():
            item[field] = values[":v" + placeholder[2:]]
        return {"Attributes": dict(item)}

    def delete_item(self, **kwargs):
        key = self._key(kwargs["Key"])
        if key not in self.items:
            raise ConditionalCheckFailedException("condition failed")
        del self.items[key]
        return {}


class FakeResource:
    """Minimal stand-in for a boto3 DynamoDB service resource."""

    def __init__(self, table):
        self._table = table
        self.requested = []

    def Table(self, name):  # noqa: N802 - mirrors the boto3 API
        self.requested.append(name)
        return self._table


@pytest.fixture()
def repository():
    return InMemoryNotesRepository()


@pytest.fixture()
def client(repository):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repository
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _create(client, title="Title", body="Body", user=None):
    headers = {"X-User-Id": user} if user else {}
    return client.post("/notes", json={"title": title, "body": body}, headers=headers)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "personal_notes_api"
    assert payload["table"]


def test_create_note(client):
    response = _create(client, title="Shopping", body="Milk and eggs")
    assert response.status_code == 201
    note = response.json()
    assert note["title"] == "Shopping"
    assert note["body"] == "Milk and eggs"
    assert note["user_id"] == "default-user"
    assert note["note_id"]
    assert note["created_at"] == note["updated_at"]


def test_create_note_validation_error(client):
    response = client.post("/notes", json={"title": "", "body": "x"})
    assert response.status_code == 422
    assert response.json() == {"detail": "Request validation failed", "code": "validation_error"}


def test_create_note_missing_body_field(client):
    response = client.post("/notes", json={"title": "Only title"})
    assert response.status_code == 422


def test_list_notes_empty(client):
    response = client.get("/notes")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_token": None, "count": 0}


def test_list_notes_with_pagination(client):
    for index in range(3):
        assert _create(client, title=f"Note {index}").status_code == 201

    first = client.get("/notes", params={"limit": 2}).json()
    assert first["count"] == 2
    assert first["next_token"]

    second = client.get("/notes", params={"limit": 2, "next_token": first["next_token"]}).json()
    assert second["count"] == 1
    assert second["next_token"] is None

    seen = {note["note_id"] for note in first["items"] + second["items"]}
    assert len(seen) == 3


def test_list_notes_invalid_token(client):
    response = client.get("/notes", params={"next_token": "!!!not-base64!!!"})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_list_notes_limit_out_of_range(client):
    assert client.get("/notes", params={"limit": 0}).status_code == 422
    assert client.get("/notes", params={"limit": 101}).status_code == 422


def test_get_note(client):
    created = _create(client).json()
    response = client.get(f"/notes/{created['note_id']}")
    assert response.status_code == 200
    assert response.json() == created


def test_get_note_not_found(client):
    response = client.get("/notes/does-not-exist")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_update_note_partial(client):
    created = _create(client, title="Old", body="Body").json()
    response = client.put(f"/notes/{created['note_id']}", json={"title": "New"})
    assert response.status_code == 200
    updated = response.json()
    assert updated["title"] == "New"
    assert updated["body"] == "Body"
    assert updated["created_at"] == created["created_at"]


def test_update_note_requires_a_field(client):
    created = _create(client).json()
    response = client.put(f"/notes/{created['note_id']}", json={})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_update_note_not_found(client):
    response = client.put("/notes/missing", json={"body": "whatever"})
    assert response.status_code == 404


def test_delete_note(client):
    created = _create(client).json()
    response = client.delete(f"/notes/{created['note_id']}")
    assert response.status_code == 204
    assert response.content == b""
    assert client.get(f"/notes/{created['note_id']}").status_code == 404


def test_delete_note_not_found(client):
    response = client.delete("/notes/missing")
    assert response.status_code == 404


def test_notes_are_scoped_per_user(client):
    alice = _create(client, title="Alice note", user="alice").json()
    _create(client, title="Bob note", user="bob")

    alice_list = client.get("/notes", headers={"X-User-Id": "alice"}).json()
    assert alice_list["count"] == 1
    assert alice_list["items"][0]["title"] == "Alice note"

    assert client.get(f"/notes/{alice['note_id']}", headers={"X-User-Id": "bob"}).status_code == 404


def test_token_roundtrip():
    key = {"user_id": "alice", "note_id": "abc"}
    assert decode_token(encode_token(key)) == key
    assert encode_token(None) is None
    assert decode_token(None) is None


def test_decode_token_rejects_non_mapping():
    token = encode_token({"user_id": "a", "note_id": "b"})
    assert token is not None
    with pytest.raises(InvalidPageTokenError):
        decode_token("@@@")


def test_dynamodb_resource_uses_endpoint(monkeypatch):
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
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "us-east-1"


def test_dynamodb_resource_without_endpoint(monkeypatch):
    captured = {}

    def fake_resource(service, **kwargs):
        captured.update(kwargs)
        return "resource"

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    storage.dynamodb_resource()
    assert captured["endpoint_url"] is None


def test_dynamodb_repository_crud():
    table = FakeTable()
    repo = DynamoDBNotesRepository(name="notes-table", resource=FakeResource(table))

    note = {
        "user_id": "alice",
        "note_id": "n1",
        "title": "T",
        "body": "B",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    assert repo.create_note(note)["note_id"] == "n1"
    assert repo.get_note("alice", "n1")["title"] == "T"

    updated = repo.update_note("alice", "n1", {"title": "T2", "updated_at": "2024-01-02T00:00:00Z"})
    assert updated["title"] == "T2"
    assert updated["updated_at"] == "2024-01-02T00:00:00Z"

    repo.delete_note("alice", "n1")
    with pytest.raises(NoteNotFoundError):
        repo.get_note("alice", "n1")


def test_dynamodb_repository_list_pagination():
    table = FakeTable()
    repo = DynamoDBNotesRepository(name="notes-table", resource=FakeResource(table))
    for index in range(3):
        repo.create_note(
            {
                "user_id": "alice",
                "note_id": f"n{index}",
                "title": "T",
                "body": "B",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        )

    items, token = repo.list_notes("alice", limit=2)
    assert [item["note_id"] for item in items] == ["n0", "n1"]
    assert token

    items, token = repo.list_notes("alice", limit=2, next_token=token)
    assert [item["note_id"] for item in items] == ["n2"]
    assert token is None
    assert table.queries[0]["Limit"] == 2


def test_dynamodb_repository_missing_items():
    table = FakeTable()
    repo = DynamoDBNotesRepository(name="notes-table", resource=FakeResource(table))

    with pytest.raises(NoteNotFoundError):
        repo.update_note("alice", "nope", {"title": "x"})
    with pytest.raises(NoteNotFoundError):
        repo.delete_note("alice", "nope")
    with pytest.raises(ValueError):
        repo.update_note("alice", "n1", {})


def test_dynamodb_repository_propagates_other_errors():
    class BrokenTable(FakeTable):
        def update_item(self, **kwargs):
            raise RuntimeError("boom")

        def delete_item(self, **kwargs):
            raise RuntimeError("boom")

    repo = DynamoDBNotesRepository(name="notes-table", resource=FakeResource(BrokenTable()))
    with pytest.raises(RuntimeError):
        repo.update_note("alice", "n1", {"title": "x"})
    with pytest.raises(RuntimeError):
        repo.delete_note("alice", "n1")


def test_default_repository_is_dynamodb(monkeypatch):
    app_module.set_repository(None)
    repo = app_module.get_repository()
    assert isinstance(repo, DynamoDBNotesRepository)
    assert app_module.get_repository() is repo
    app_module.set_repository(None)


def test_get_user_id_fallback():
    assert app_module.get_user_id("   ") == "default-user"
    assert app_module.get_user_id("alice") == "alice"
    assert app_module.get_user_id(None) == "default-user"
