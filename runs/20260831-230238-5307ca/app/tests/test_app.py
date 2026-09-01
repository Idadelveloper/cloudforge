"""Offline tests for the bookmark manager API."""

import os
import sys

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage as storage_module  # noqa: E402

API_KEY = "unit-test-key"
HEADERS = {"X-API-Key": API_KEY}


class FakeRepository:
    """In-memory stand-in for the DynamoDB repository."""

    def __init__(self):
        self.table_name = "bookmarks"
        self.items = {}
        self.counter = 0

    def create(self, item):
        self.counter += 1
        stored = dict(item)
        stored["created_at"] = "2024-01-01T00:00:{0:02d}Z".format(self.counter)
        stored["updated_at"] = stored["created_at"]
        self.items[stored["bookmark_id"]] = stored
        return dict(stored)

    def get(self, bookmark_id):
        item = self.items.get(bookmark_id)
        return dict(item) if item else None

    def delete(self, bookmark_id):
        return self.items.pop(bookmark_id, None) is not None

    def list_bookmarks(self, tag=None, limit=50):
        items = list(self.items.values())
        if tag:
            items = [entry for entry in items if tag in entry.get("tags", [])]
        items.sort(key=lambda entry: entry.get("created_at", ""), reverse=True)
        return [dict(entry) for entry in items[:limit]]


class FakeKeyProvider:
    """Deterministic API key provider."""

    def __init__(self, key=API_KEY):
        self.key = key
        self.calls = 0

    def get_api_key(self, force_refresh=False):
        self.calls += 1
        return self.key


class BrokenKeyProvider:
    """Provider that always fails, simulating a Secrets Manager outage."""

    def get_api_key(self, force_refresh=False):
        raise RuntimeError("secrets manager unavailable")


@pytest.fixture
def repository():
    return FakeRepository()


@pytest.fixture
def client(repository):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repository
    app_module.app.dependency_overrides[app_module.get_api_key_provider] = lambda: FakeKeyProvider()
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _create(client, url="https://example.com/a", title="Example A", tags=None):
    payload = {"url": url, "title": title}
    if tags is not None:
        payload["tags"] = tags
    return client.post("/bookmarks", json=payload, headers=HEADERS)


def test_health_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "bookmark_manager_api"
    assert body["table"] == "bookmarks"


def test_missing_api_key_returns_401(client):
    response = client.get("/bookmarks")
    assert response.status_code == 401
    assert response.json()["status_code"] == 401


def test_wrong_api_key_returns_403(client):
    response = client.get("/bookmarks", headers={"X-API-Key": "nope"})
    assert response.status_code == 403
    assert "Invalid" in response.json()["detail"]


def test_secrets_failure_returns_503(repository):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repository
    app_module.app.dependency_overrides[app_module.get_api_key_provider] = BrokenKeyProvider
    with TestClient(app_module.app) as broken_client:
        response = broken_client.get("/bookmarks", headers=HEADERS)
    app_module.app.dependency_overrides.clear()
    assert response.status_code == 503


def test_create_bookmark(client, repository):
    response = _create(client, tags=["Python", "python", " Docs "])
    assert response.status_code == 201
    body = response.json()
    assert body["url"] == "https://example.com/a"
    assert body["title"] == "Example A"
    assert body["tags"] == ["python", "docs"]
    assert body["bookmark_id"]
    stored = repository.items[body["bookmark_id"]]
    assert stored["tag"] == "python"


def test_create_rejects_invalid_url(client):
    response = _create(client, url="not-a-url")
    assert response.status_code == 422
    body = response.json()
    assert body["status_code"] == 422
    assert isinstance(body["detail"], str)


def test_create_failure_returns_502(client, repository):
    def boom(_item):
        raise RuntimeError("dynamo down")

    repository.create = boom
    response = _create(client)
    assert response.status_code == 502


def test_list_and_tag_filter(client):
    _create(client, url="https://example.com/one", title="One", tags=["news"])
    _create(client, url="https://example.com/two", title="Two", tags=["dev", "news"])
    _create(client, url="https://example.com/three", title="Three", tags=[])

    listing = client.get("/bookmarks", headers=HEADERS)
    assert listing.status_code == 200
    assert listing.json()["count"] == 3
    assert listing.json()["tag"] is None

    filtered = client.get("/bookmarks", params={"tag": "NEWS"}, headers=HEADERS)
    assert filtered.status_code == 200
    body = filtered.json()
    assert body["tag"] == "news"
    assert body["count"] == 2
    titles = sorted(item["title"] for item in body["items"])
    assert titles == ["One", "Two"]

    limited = client.get("/bookmarks", params={"limit": 1}, headers=HEADERS)
    assert limited.json()["count"] == 1


def test_get_bookmark_roundtrip(client):
    created = _create(client, tags=["read"]).json()
    fetched = client.get("/bookmarks/{0}".format(created["bookmark_id"]), headers=HEADERS)
    assert fetched.status_code == 200
    assert fetched.json()["bookmark_id"] == created["bookmark_id"]


def test_get_missing_bookmark_returns_404(client):
    response = client.get("/bookmarks/does-not-exist", headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "Bookmark not found"


def test_delete_bookmark(client):
    created = _create(client).json()
    deleted = client.delete("/bookmarks/{0}".format(created["bookmark_id"]), headers=HEADERS)
    assert deleted.status_code == 204
    assert deleted.content in (b"", None)

    again = client.delete("/bookmarks/{0}".format(created["bookmark_id"]), headers=HEADERS)
    assert again.status_code == 404


# --------------------------------------------------------------------------- #
# storage layer (boto3 fully stubbed)
# --------------------------------------------------------------------------- #


class FakeTable:
    """Minimal stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self):
        self.items = {}
        self.scan_kwargs = []

    def put_item(self, Item):  # noqa: N803 - boto3 keyword name
        self.items[Item["bookmark_id"]] = dict(Item)
        return {}

    def get_item(self, Key):  # noqa: N803 - boto3 keyword name
        item = self.items.get(Key["bookmark_id"])
        return {"Item": dict(item)} if item else {}

    def delete_item(self, **kwargs):
        key = kwargs["Key"]["bookmark_id"]
        if key not in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "missing"}},
                "DeleteItem",
            )
        del self.items[key]
        return {}

    def scan(self, **kwargs):
        self.scan_kwargs.append(kwargs)
        return {"Items": [dict(item) for item in self.items.values()]}


def _repo_with_table():
    repo = storage_module.BookmarkRepository(table_name="bookmarks")
    table = FakeTable()
    repo._table = table
    return repo, table


def test_repository_crud_with_fake_table():
    repo, table = _repo_with_table()
    repo.create({"bookmark_id": "b1", "url": "https://x.dev", "title": "X", "tags": ["dev"],
                 "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z"})
    assert repo.get("b1")["title"] == "X"
    assert repo.get("missing") is None
    assert repo.delete("b1") is True
    assert repo.delete("b1") is False
    assert table.items == {}


def test_repository_list_passes_filter_expression():
    repo, table = _repo_with_table()
    repo.create({"bookmark_id": "b1", "created_at": "2024-01-01T00:00:00Z", "tags": ["dev"]})
    repo.create({"bookmark_id": "b2", "created_at": "2024-01-02T00:00:00Z", "tags": ["dev"]})

    items = repo.list_bookmarks()
    assert [item["bookmark_id"] for item in items] == ["b2", "b1"]
    assert "FilterExpression" not in table.scan_kwargs[0]

    repo.list_bookmarks(tag="dev", limit=1)
    assert "FilterExpression" in table.scan_kwargs[1]


def test_dynamodb_resource_uses_endpoint(monkeypatch):
    captured = {}

    def fake_resource(service_name, **kwargs):
        captured["service"] = service_name
        captured.update(kwargs)
        return "resource"

    monkeypatch.setattr(storage_module.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    assert storage_module.dynamodb_resource() == "resource"
    assert captured["service"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "us-east-1"


def test_secrets_client_uses_env(monkeypatch):
    captured = {}

    def fake_client(service_name, **kwargs):
        captured["service"] = service_name
        captured.update(kwargs)
        return "client"

    monkeypatch.setattr(storage_module.boto3, "client", fake_client)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    assert storage_module.secretsmanager_client() == "client"
    assert captured["service"] == "secretsmanager"
    assert captured["endpoint_url"] is None
    assert captured["region_name"] == "eu-west-1"


class FakeSecretsClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3 keyword name
        self.calls += 1
        return {"SecretString": self.payload}


def test_api_key_provider_parses_json_and_caches():
    fake = FakeSecretsClient('{"api_key": "json-key"}')
    provider = storage_module.ApiKeyProvider(
        secret_name="bookmark-manager/api-key",
        ttl_seconds=60,
        client_factory=lambda: fake,
    )
    assert provider.get_api_key() == "json-key"
    assert provider.get_api_key() == "json-key"
    assert fake.calls == 1

    fake.payload = '{"api_key": "rotated"}'
    assert provider.get_api_key(force_refresh=True) == "rotated"
    assert fake.calls == 2


def test_api_key_provider_accepts_plaintext():
    provider = storage_module.ApiKeyProvider(
        ttl_seconds=60,
        client_factory=lambda: FakeSecretsClient("  plain-key  "),
    )
    assert provider.get_api_key() == "plain-key"


def test_api_key_provider_falls_back_to_env(monkeypatch):
    def broken_factory():
        raise RuntimeError("no secrets manager")

    monkeypatch.setenv("BOOKMARK_API_KEY", "env-key")
    provider = storage_module.ApiKeyProvider(ttl_seconds=60, client_factory=broken_factory)
    assert provider.get_api_key() == "env-key"


def test_api_key_provider_returns_none_without_sources(monkeypatch):
    def broken_factory():
        raise RuntimeError("no secrets manager")

    monkeypatch.delenv("BOOKMARK_API_KEY", raising=False)
    provider = storage_module.ApiKeyProvider(ttl_seconds=60, client_factory=broken_factory)
    assert provider.get_api_key() is None
