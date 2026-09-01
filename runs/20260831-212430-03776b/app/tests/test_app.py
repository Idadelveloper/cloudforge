"""Offline tests for the contact-form backend. No AWS or network access."""

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app as app_module
import storage

ADMIN_KEY_VALUE = "unit-test-admin-value"
ADMIN_HEADER = {"X-Admin-API-Key": ADMIN_KEY_VALUE}


class FakeTable:
    """Minimal stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, fail=False):
        self.items = {}
        self.fail = fail
        self.last_scan_kwargs = None

    def _maybe_fail(self, op):
        if self.fail:
            raise ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, op)

    def put_item(self, Item=None, **kwargs):  # noqa: N803 - boto3 style kwargs
        self._maybe_fail("PutItem")
        self.items[Item["message_id"]] = dict(Item)
        return {}

    def get_item(self, Key=None, **kwargs):  # noqa: N803
        self._maybe_fail("GetItem")
        item = self.items.get(Key["message_id"])
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key=None, **kwargs):  # noqa: N803
        self._maybe_fail("DeleteItem")
        item = self.items.pop(Key["message_id"], None)
        return {"Attributes": item} if item else {}

    def scan(self, **kwargs):
        self._maybe_fail("Scan")
        self.last_scan_kwargs = kwargs
        values = [dict(v) for v in self.items.values()]
        limit = kwargs.get("Limit", len(values) or 1)
        page = values[:limit]
        result = {"Items": page}
        if len(values) > limit:
            result["LastEvaluatedKey"] = {"message_id": page[-1]["message_id"]}
        return result


def seed_item(index, message_id=None):
    return {
        "message_id": message_id or "id-%d" % index,
        "name": "Visitor %d" % index,
        "email": "visitor%d@example.com" % index,
        "message": "Hello number %d" % index,
        "created_at": "2024-01-0%dT10:00:00Z" % index,
    }


@pytest.fixture()
def repo():
    return storage.InMemoryMessageRepository()


@pytest.fixture()
def client(repo, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY_VALUE)
    monkeypatch.setenv("MESSAGES_TABLE", "test-contact-form-messages")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


# ------------------------------- health ----------------------------------- #
def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dynamodb"] == "reachable"
    assert body["table"] == "test-contact-form-messages"


def test_health_degraded_when_datastore_unreachable(client, repo):
    repo.healthy = False
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["dynamodb"] == "unreachable"


# ---------------------------- create message ------------------------------ #
def test_create_message_persists_item(client, repo):
    payload = {"name": "  Ada Lovelace ", "email": "ada@example.com", "message": " Hi there "}
    response = client.post("/messages", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Ada Lovelace"
    assert body["message"] == "Hi there"
    assert body["email"] == "ada@example.com"
    assert body["created_at"].endswith("Z")
    stored = repo.get_message(body["message_id"])
    assert stored is not None
    assert stored["email"] == "ada@example.com"


def test_create_message_rejects_invalid_email(client):
    response = client.post(
        "/messages",
        json={"name": "Bob", "email": "not-an-email", "message": "hello"},
    )
    assert response.status_code == 422


def test_create_message_rejects_missing_fields(client):
    response = client.post("/messages", json={"email": "a@b.com"})
    assert response.status_code == 422


def test_create_message_rejects_blank_name(client):
    response = client.post(
        "/messages",
        json={"name": "   ", "email": "a@b.com", "message": "hello"},
    )
    assert response.status_code == 422


def test_create_message_rejects_blank_body(client):
    response = client.post(
        "/messages",
        json={"name": "Bob", "email": "a@b.com", "message": "   "},
    )
    assert response.status_code == 422


def test_create_message_storage_failure_returns_503(client, monkeypatch, repo):
    def boom(item):
        raise storage.StorageError("table gone")

    monkeypatch.setattr(repo, "put_message", boom)
    response = client.post(
        "/messages",
        json={"name": "Bob", "email": "a@b.com", "message": "hello"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "storage_error"


# ------------------------------- admin auth ------------------------------- #
def test_list_requires_api_key(client):
    assert client.get("/messages").status_code == 401
    assert client.get("/messages", headers={"X-Admin-API-Key": "nope"}).status_code == 401


def test_delete_requires_api_key(client):
    assert client.delete("/messages/id-1").status_code == 401


def test_admin_endpoint_503_when_key_unconfigured(client, monkeypatch):
    monkeypatch.setattr(app_module, "resolve_admin_api_key", lambda: None)
    response = client.get("/messages", headers=ADMIN_HEADER)
    assert response.status_code == 503


# ------------------------------ list messages ----------------------------- #
def test_list_messages_newest_first_and_paginates(client, repo):
    for index in (1, 2, 3):
        repo.put_message(seed_item(index))

    first = client.get("/messages?limit=2", headers=ADMIN_HEADER)
    assert first.status_code == 200
    body = first.json()
    assert body["count"] == 2
    assert [item["message_id"] for item in body["items"]] == ["id-3", "id-2"]
    assert body["next_token"]

    second = client.get(
        "/messages",
        params={"limit": 2, "next_token": body["next_token"]},
        headers=ADMIN_HEADER,
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["count"] == 1
    assert second_body["items"][0]["message_id"] == "id-1"
    assert second_body["next_token"] is None


def test_list_messages_rejects_bad_token(client):
    response = client.get("/messages", params={"next_token": "!!!not-base64!!!"}, headers=ADMIN_HEADER)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_next_token"


def test_list_messages_validates_limit(client):
    assert client.get("/messages?limit=0", headers=ADMIN_HEADER).status_code == 422
    assert client.get("/messages?limit=500", headers=ADMIN_HEADER).status_code == 422


# ------------------------------ get message ------------------------------- #
def test_get_message_found_and_missing(client, repo):
    repo.put_message(seed_item(1))
    found = client.get("/messages/id-1", headers=ADMIN_HEADER)
    assert found.status_code == 200
    assert found.json()["email"] == "visitor1@example.com"

    missing = client.get("/messages/does-not-exist", headers=ADMIN_HEADER)
    assert missing.status_code == 404


# ----------------------------- delete message ----------------------------- #
def test_delete_message(client, repo):
    repo.put_message(seed_item(2))
    deleted = client.delete("/messages/id-2", headers=ADMIN_HEADER)
    assert deleted.status_code == 200
    assert deleted.json() == {"message_id": "id-2", "deleted": True}
    assert repo.get_message("id-2") is None
    assert client.delete("/messages/id-2", headers=ADMIN_HEADER).status_code == 404


# --------------------------- storage unit tests --------------------------- #
def test_token_roundtrip():
    token = storage.encode_token({"message_id": "abc"})
    assert storage.decode_token(token) == {"message_id": "abc"}
    assert storage.encode_token(None) is None
    assert storage.decode_token(None) is None
    with pytest.raises(storage.InvalidTokenError):
        storage.decode_token("%%%")


def test_dynamodb_repository_crud_with_fake_table():
    table = FakeTable()
    repo = storage.DynamoDBMessageRepository(table_name="t", table=table)
    assert repo.health() is True
    repo.put_message(seed_item(1))
    repo.put_message(seed_item(2))
    assert repo.get_message("id-1")["name"] == "Visitor 1"
    assert repo.get_message("missing") is None

    items, token = repo.list_messages(limit=1)
    assert len(items) == 1
    assert token
    next_items, _ = repo.list_messages(limit=5, next_token=token)
    assert len(next_items) == 2
    assert table.last_scan_kwargs["ExclusiveStartKey"] == {"message_id": "id-1"}

    assert repo.delete_message("id-1") is True
    assert repo.delete_message("id-1") is False


def test_dynamodb_repository_wraps_client_errors():
    repo = storage.DynamoDBMessageRepository(table_name="t", table=FakeTable(fail=True))
    assert repo.health() is False
    with pytest.raises(storage.StorageError):
        repo.put_message(seed_item(1))
    with pytest.raises(storage.StorageError):
        repo.get_message("id-1")
    with pytest.raises(storage.StorageError):
        repo.delete_message("id-1")
    with pytest.raises(storage.StorageError):
        repo.list_messages()


def test_dynamodb_resource_uses_endpoint_override(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # noqa: S105
    resource = storage.dynamodb_resource()
    assert resource.meta.client.meta.endpoint_url == "http://localhost:4566"


# --------------------------- admin key resolution ------------------------- #
def test_resolve_admin_api_key_prefers_environment(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "from-env")
    assert app_module.resolve_admin_api_key() == "from-env"


def test_resolve_admin_api_key_reads_secrets_manager(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.setattr(app_module, "_ADMIN_KEY_CACHE", None, raising=False)

    class FakeSecrets:
        def get_secret_value(self, SecretId=None):  # noqa: N803
            assert SecretId == storage.DEFAULT_SECRET_NAME
            return {"SecretString": '{"admin_api_key": "from-secrets"}'}

    monkeypatch.setattr(app_module, "secretsmanager_client", lambda: FakeSecrets())
    assert app_module.resolve_admin_api_key() == "from-secrets"
    monkeypatch.setattr(app_module, "_ADMIN_KEY_CACHE", None, raising=False)


def test_resolve_admin_api_key_handles_secret_failure(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.setattr(app_module, "_ADMIN_KEY_CACHE", None, raising=False)

    def broken_client():
        raise RuntimeError("no aws here")

    monkeypatch.setattr(app_module, "secretsmanager_client", broken_client)
    assert app_module.resolve_admin_api_key() is None
    monkeypatch.setattr(app_module, "_ADMIN_KEY_CACHE", None, raising=False)


def test_get_repository_returns_dynamodb_repository(monkeypatch):
    monkeypatch.setattr(app_module, "_REPOSITORY", None, raising=False)
    monkeypatch.setenv("MESSAGES_TABLE", "custom-table")
    created = app_module.get_repository()
    assert isinstance(created, storage.DynamoDBMessageRepository)
    assert created.table_name == "custom-table"
    assert app_module.get_repository() is created
    monkeypatch.setattr(app_module, "_REPOSITORY", None, raising=False)
