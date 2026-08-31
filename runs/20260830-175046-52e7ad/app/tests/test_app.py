"""Offline tests for the personal notes API (no AWS/network access)."""
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app as app_module
import storage
from app import app, get_repository, reset_repository
from storage import (
    DynamoDBNotesRepository,
    InMemoryNotesRepository,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)


@pytest.fixture()
def repo():
    return InMemoryNotesRepository()


@pytest.fixture()
def client(repo):
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _create(client, title="first", body="hello", user=None):
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
    response = _create(client)
    assert response.status_code == 201
    note = response.json()
    assert note["title"] == "first"
    assert note["body"] == "hello"
    assert note["user_id"] == "default-user"
    assert note["note_id"]
    assert note["created_at"] == note["updated_at"]


def test_create_note_defaults_empty_body(client):
    response = client.post("/notes", json={"title": "only title"})
    assert response.status_code == 201
    assert response.json()["body"] == ""


def test_create_note_validation_error(client):
    response = client.post("/notes", json={"title": ""})
    assert response.status_code == 422
    assert response.json() == {"detail": "request validation failed", "code": "validation_error"}


def test_get_note(client):
    note_id = _create(client).json()["note_id"]
    response = client.get("/notes/{0}".format(note_id))
    assert response.status_code == 200
    assert response.json()["note_id"] == note_id


def test_get_note_not_found(client):
    response = client.get("/notes/missing")
    assert response.status_code == 404
    assert response.json() == {"detail": "note not found", "code": "note_not_found"}


def test_list_notes_and_pagination(client):
    for index in range(3):
        assert _create(client, title="note-{0}".format(index)).status_code == 201

    first = client.get("/notes", params={"limit": 2})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["count"] == 2
    assert len(first_body["items"]) == 2
    assert first_body["next_cursor"]

    second = client.get("/notes", params={"limit": 2, "cursor": first_body["next_cursor"]})
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["count"] == 1
    assert second_body["next_cursor"] is None

    seen = {item["note_id"] for item in first_body["items"] + second_body["items"]}
    assert len(seen) == 3


def test_list_notes_invalid_cursor(client):
    response = client.get("/notes", params={"cursor": "!!!not-base64!!!"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


def test_list_notes_limit_bounds(client):
    assert client.get("/notes", params={"limit": 0}).status_code == 422
    assert client.get("/notes", params={"limit": 1000}).status_code == 422


def test_notes_are_scoped_per_user(client):
    _create(client, title="alice note", user="alice")
    default_list = client.get("/notes").json()
    assert default_list["count"] == 0
    alice_list = client.get("/notes", headers={"X-User-Id": "alice"}).json()
    assert alice_list["count"] == 1
    assert alice_list["items"][0]["user_id"] == "alice"


def test_update_note(client):
    created = _create(client).json()
    response = client.put(
        "/notes/{0}".format(created["note_id"]),
        json={"title": "updated title"},
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["title"] == "updated title"
    assert updated["body"] == created["body"]
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]


def test_update_note_requires_fields(client):
    created = _create(client).json()
    response = client.put("/notes/{0}".format(created["note_id"]), json={})
    assert response.status_code == 400
    assert response.json()["code"] == "no_fields_to_update"


def test_update_note_not_found(client):
    response = client.put("/notes/missing", json={"title": "nope"})
    assert response.status_code == 404
    assert response.json()["code"] == "note_not_found"


def test_delete_note(client):
    note_id = _create(client).json()["note_id"]
    response = client.delete("/notes/{0}".format(note_id))
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/notes/{0}".format(note_id)).status_code == 404
    assert client.delete("/notes/{0}".format(note_id)).status_code == 404


def test_cursor_round_trip():
    key = {"user_id": "u", "note_id": "n"}
    assert decode_cursor(encode_cursor(key)) == key
    with pytest.raises(InvalidCursorError):
        decode_cursor("###")
    with pytest.raises(InvalidCursorError):
        decode_cursor(encode_cursor({}))


class FakeTable:
    """Minimal stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self):
        self.items = {}
        self.query_response = {"Items": []}
        self.last_query_params = None
        self.conditional_failure = False
        self.other_failure = False

    @staticmethod
    def _pk(key):
        return (key["user_id"], key["note_id"])

    def _raise(self, operation):
        code = "ConditionalCheckFailedException" if self.conditional_failure else "ProvisionedThroughputExceeded"
        raise ClientError({"Error": {"Code": code, "Message": "fake"}}, operation)

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[(item["user_id"], item["note_id"])] = dict(item)
        return {}

    def get_item(self, **kwargs):
        item = self.items.get(self._pk(kwargs["Key"]))
        return {"Item": dict(item)} if item else {}

    def query(self, **kwargs):
        self.last_query_params = kwargs
        return self.query_response

    def update_item(self, **kwargs):
        if self.conditional_failure or self.other_failure:
            self._raise("UpdateItem")
        pk = self._pk(kwargs["Key"])
        item = self.items.setdefault(pk, dict(kwargs["Key"]))
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        for alias, field in names.items():
            item[field] = values[":v{0}".format(alias[2:])]
        return {"Attributes": dict(item)}

    def delete_item(self, **kwargs):
        if self.conditional_failure or self.other_failure:
            self._raise("DeleteItem")
        self.items.pop(self._pk(kwargs["Key"]), None)
        return {}


@pytest.fixture()
def dynamo():
    table = FakeTable()
    return DynamoDBNotesRepository(table=table, name="test-notes"), table


def test_dynamo_repository_crud(dynamo):
    repository, table = dynamo
    note = {
        "user_id": "u1",
        "note_id": "n1",
        "title": "t",
        "body": "b",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    assert repository.create(note) == note
    assert repository.get("u1", "n1")["title"] == "t"
    assert repository.get("u1", "missing") is None

    updated = repository.update("u1", "n1", {"title": "new", "updated_at": "2024-01-02T00:00:00Z", "bad": "x"})
    assert updated["title"] == "new"
    assert updated["updated_at"] == "2024-01-02T00:00:00Z"
    assert "bad" not in updated

    assert repository.update("u1", "n1", {})["title"] == "new"
    assert repository.delete("u1", "n1") is True
    assert table.items == {}


def test_dynamo_repository_list(dynamo):
    repository, table = dynamo
    table.query_response = {
        "Items": [{"user_id": "u1", "note_id": "n1"}],
        "LastEvaluatedKey": {"user_id": "u1", "note_id": "n1"},
    }
    items, cursor = repository.list("u1", limit=1)
    assert items == [{"user_id": "u1", "note_id": "n1"}]
    assert cursor == encode_cursor({"user_id": "u1", "note_id": "n1"})
    assert table.last_query_params["Limit"] == 1
    assert "ExclusiveStartKey" not in table.last_query_params

    table.query_response = {"Items": []}
    items, cursor = repository.list("u1", limit=5, cursor=cursor)
    assert items == []
    assert cursor is None
    assert table.last_query_params["ExclusiveStartKey"] == {"user_id": "u1", "note_id": "n1"}


def test_dynamo_repository_conditional_failures(dynamo):
    repository, table = dynamo
    table.conditional_failure = True
    assert repository.update("u1", "n1", {"title": "x"}) is None
    assert repository.delete("u1", "n1") is False


def test_dynamo_repository_other_client_errors(dynamo):
    repository, table = dynamo
    table.other_failure = True
    with pytest.raises(ClientError):
        repository.update("u1", "n1", {"title": "x"})
    with pytest.raises(ClientError):
        repository.delete("u1", "n1")


def test_get_repository_is_cached_and_offline(monkeypatch):
    monkeypatch.setenv("NOTES_TABLE_NAME", "env-notes-table")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    reset_repository()
    try:
        first = get_repository()
        second = get_repository()
        assert first is second
        assert isinstance(first, DynamoDBNotesRepository)
        assert first.name == "env-notes-table"
        assert storage.table_name() == "env-notes-table"
    finally:
        reset_repository()


def test_resolve_user_id_trims_header():
    assert app_module.resolve_user_id("  bob  ") == "bob"
    assert app_module.resolve_user_id("   ") == "default-user"
    assert app_module.resolve_user_id(None) == "default-user"
