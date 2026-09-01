"""Offline tests for the contact-form backend.

All AWS access is replaced by in-memory fakes injected through FastAPI
dependency overrides, so no network or LocalStack instance is required.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, get_key_provider, get_repository  # noqa: E402
from storage import (  # noqa: E402
    AdminKeyProvider,
    DynamoDBMessageRepository,
    InMemoryMessageRepository,
    _extract_secret_value,
    decode_cursor,
    encode_cursor,
)

TEST_ADMIN_KEY = "unit-test-admin-key"
ADMIN_HEADERS = {"X-Api-Key": TEST_ADMIN_KEY}


class FakeKeyProvider:
    """Stand-in for AdminKeyProvider."""

    def __init__(self, key=TEST_ADMIN_KEY, error=None):
        self.key = key
        self.error = error

    def get_key(self):
        if self.error is not None:
            raise self.error
        return self.key


class FakeDynamoTable:
    """Minimal stub of a boto3 DynamoDB Table resource."""

    table_status = "ACTIVE"

    def __init__(self):
        self.items = {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item["message_id"]] = dict(item)
        return {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["message_id"])
        return {"Item": dict(item)} if item else {}

    def delete_item(self, **kwargs):
        item = self.items.pop(kwargs["Key"]["message_id"], None)
        return {"Attributes": item} if item else {}

    def scan(self, **kwargs):
        items = list(self.items.values())
        start = 0
        start_key = kwargs.get("ExclusiveStartKey")
        if start_key:
            for index, item in enumerate(items):
                if item["message_id"] == start_key["message_id"]:
                    start = index + 1
                    break
        limit = kwargs.get("Limit", len(items))
        page = items[start:start + limit]
        response = {"Items": page}
        if page and start + limit < len(items):
            response["LastEvaluatedKey"] = {"message_id": page[-1]["message_id"]}
        return response


class FakeSecretsClient:
    def __init__(self, secret_string):
        self.secret_string = secret_string
        self.calls = 0

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3 signature
        self.calls += 1
        return {"Name": SecretId, "SecretString": self.secret_string}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def repository():
    return InMemoryMessageRepository(name="contact-form-messages")


@pytest.fixture
def client(repository):
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_key_provider] = lambda: FakeKeyProvider()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _submit(client, name="Ada Lovelace", email="ada@example.com", message="Hello there"):
    return client.post("/messages", json={"name": name, "email": email, "message": message})


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["table"] == "contact-form-messages"
    assert body["table_reachable"] is True


def test_health_degraded_when_table_unreachable():
    broken = InMemoryMessageRepository(name="contact-form-messages", reachable=False)
    app.dependency_overrides[get_repository] = lambda: broken
    app.dependency_overrides[get_key_provider] = lambda: FakeKeyProvider()
    with TestClient(app) as test_client:
        body = test_client.get("/health").json()
    app.dependency_overrides.clear()
    assert body["status"] == "degraded"
    assert body["table_reachable"] is False


def test_create_message_success(client, repository):
    response = _submit(client)
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Ada Lovelace"
    assert body["email"] == "ada@example.com"
    assert body["message"] == "Hello there"
    assert body["message_id"]
    assert body["created_at"].endswith("Z")
    assert repository.get_message(body["message_id"]) is not None


def test_create_message_normalises_input(client):
    body = _submit(client, name="  Grace  ", email="  GRACE@Example.COM ").json()
    assert body["name"] == "Grace"
    assert body["email"] == "grace@example.com"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "", "email": "a@b.com", "message": "hi"},
        {"name": "   ", "email": "a@b.com", "message": "hi"},
        {"name": "A", "email": "not-an-email", "message": "hi"},
        {"name": "A", "email": "a@b.com", "message": ""},
        {"name": "A" * 101, "email": "a@b.com", "message": "hi"},
        {"name": "A", "email": "a@b.com", "message": "x" * 5001},
        {"email": "a@b.com", "message": "hi"},
    ],
)
def test_create_message_validation_errors(client, payload):
    response = client.post("/messages", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["detail"], str)


def test_create_message_storage_failure(client, repository):
    def boom(_item):
        raise RuntimeError("dynamodb down")

    repository.put_message = boom
    response = _submit(client)
    assert response.status_code == 502
    assert response.json()["code"] == "storage_error"


def test_list_requires_api_key(client):
    _submit(client)
    unauth = client.get("/messages")
    assert unauth.status_code == 401
    assert unauth.json()["code"] == "unauthorized"

    wrong = client.get("/messages", headers={"X-Api-Key": "nope"})
    assert wrong.status_code == 401


def test_list_messages_and_pagination(client):
    created = [_submit(client, name="User {0}".format(i)).json() for i in range(3)]
    assert len(created) == 3

    first = client.get("/messages", params={"limit": 2}, headers=ADMIN_HEADERS)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["count"] == 2
    assert len(first_body["items"]) == 2
    cursor = first_body["next_cursor"]
    assert cursor

    second = client.get(
        "/messages",
        params={"limit": 2, "cursor": cursor},
        headers=ADMIN_HEADERS,
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["count"] == 1
    assert second_body["next_cursor"] is None

    seen = {item["message_id"] for item in first_body["items"] + second_body["items"]}
    assert seen == {item["message_id"] for item in created}


def test_list_invalid_cursor(client):
    response = client.get("/messages", params={"cursor": "!!!not-base64!!!"}, headers=ADMIN_HEADERS)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


def test_list_limit_out_of_range(client):
    response = client.get("/messages", params={"limit": 0}, headers=ADMIN_HEADERS)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_get_message_roundtrip_and_404(client):
    created = _submit(client).json()

    found = client.get("/messages/{0}".format(created["message_id"]), headers=ADMIN_HEADERS)
    assert found.status_code == 200
    assert found.json()["message_id"] == created["message_id"]

    missing = client.get("/messages/does-not-exist", headers=ADMIN_HEADERS)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"

    unauth = client.get("/messages/{0}".format(created["message_id"]))
    assert unauth.status_code == 401


def test_delete_message(client, repository):
    created = _submit(client).json()
    message_id = created["message_id"]

    unauth = client.delete("/messages/{0}".format(message_id))
    assert unauth.status_code == 401

    deleted = client.delete("/messages/{0}".format(message_id), headers=ADMIN_HEADERS)
    assert deleted.status_code == 200
    assert deleted.json() == {"message_id": message_id, "deleted": True}
    assert repository.get_message(message_id) is None

    again = client.delete("/messages/{0}".format(message_id), headers=ADMIN_HEADERS)
    assert again.status_code == 404
    assert again.json()["code"] == "not_found"


def test_admin_key_unavailable(repository):
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_key_provider] = lambda: FakeKeyProvider(key=None)
    with TestClient(app) as test_client:
        response = test_client.get("/messages", headers=ADMIN_HEADERS)
    app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["code"] == "admin_key_unavailable"


def test_admin_key_lookup_error(repository):
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_key_provider] = lambda: FakeKeyProvider(error=RuntimeError("secrets down"))
    with TestClient(app) as test_client:
        response = test_client.get("/messages", headers=ADMIN_HEADERS)
    app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["code"] == "admin_key_unavailable"


def test_dynamodb_repository_with_fake_table():
    table = FakeDynamoTable()
    repo = DynamoDBMessageRepository(table_name="contact-form-messages", table=table)

    stored = repo.put_message(
        {
            "message_id": "m-1",
            "name": "Ada",
            "email": "ada@example.com",
            "message": "hi",
            "created_at": "2024-01-01T00:00:00Z",
            "source_ip": None,
        }
    )
    assert stored["message_id"] == "m-1"
    assert "source_ip" not in table.items["m-1"]

    repo.put_message(
        {
            "message_id": "m-2",
            "name": "Grace",
            "email": "grace@example.com",
            "message": "hello",
            "created_at": "2024-01-02T00:00:00Z",
            "source_ip": "10.0.0.1",
        }
    )

    assert repo.get_message("m-1")["name"] == "Ada"
    assert repo.get_message("missing") is None

    page, last_key = repo.list_messages(limit=1)
    assert len(page) == 1
    assert last_key == {"message_id": page[0]["message_id"]}

    rest, last_key2 = repo.list_messages(limit=1, cursor=last_key)
    assert len(rest) == 1
    assert last_key2 is None

    health = repo.health()
    assert health["reachable"] is True
    assert health["table"] == "contact-form-messages"

    assert repo.delete_message("m-1") is True
    assert repo.delete_message("m-1") is False


def test_dynamodb_repository_health_failure():
    class BrokenTable:
        @property
        def table_status(self):
            raise RuntimeError("no such table")

    repo = DynamoDBMessageRepository(table_name="t", table=BrokenTable())
    health = repo.health()
    assert health["reachable"] is False
    assert "error" in health


def test_cursor_roundtrip_and_rejection():
    key = {"message_id": "abc-123"}
    assert decode_cursor(encode_cursor(key)) == key
    with pytest.raises(Exception):
        decode_cursor("@@@")


def test_admin_key_provider_reads_secret(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    fake = FakeSecretsClient('{"api_key": "from-secrets-manager"}')
    provider = AdminKeyProvider(secret_id="contact-form/admin-api-key", client=fake)
    assert provider.get_key() == "from-secrets-manager"
    assert provider.get_key() == "from-secrets-manager"
    assert fake.calls == 1

    provider.invalidate()
    assert provider.get_key() == "from-secrets-manager"
    assert fake.calls == 2


def test_admin_key_provider_prefers_environment(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "env-value")
    fake = FakeSecretsClient("unused")
    provider = AdminKeyProvider(client=fake)
    assert provider.get_key() == "env-value"
    assert fake.calls == 0


def test_extract_secret_value_variants():
    assert _extract_secret_value("plain") == "plain"
    assert _extract_secret_value('{"admin_api_key": "k1"}') == "k1"
    assert _extract_secret_value('{"other": "x"}') == ""
    assert _extract_secret_value("") == ""
