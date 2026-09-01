"""Offline tests for the bookmark manager API.

Every AWS interaction is replaced by a fake: the HTTP layer uses an in-memory
repository plus a static key provider, and the DynamoDB / Secrets Manager code
paths are exercised against hand-written stub clients.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import storage
from app import app, get_api_key_provider, get_repository
from storage import (
    DynamoBookmarkRepository,
    InMemoryBookmarkRepository,
    SecretsManagerApiKeyProvider,
    StaticApiKeyProvider,
    TokenError,
    decode_token,
    encode_token,
    parse_api_key_payload,
)

TEST_API_KEY = "unit-test-api-key"
AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def repo():
    return InMemoryBookmarkRepository()


@pytest.fixture
def client(repo):
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_api_key_provider] = lambda: StaticApiKeyProvider(TEST_API_KEY)
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


def _create(client, url="https://example.com/a", title="Example", tags=None):
    payload = {"url": url, "title": title, "tags": tags if tags is not None else ["Python", "web"]}
    return client.post("/bookmarks", json=payload, headers=AUTH_HEADERS)


def test_health_is_public_and_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dynamodb"] == "reachable"
    assert body["table"]


def test_health_reports_degraded_when_store_unreachable(client, repo):
    repo.healthy = False
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["dynamodb"] == "unreachable"


def test_missing_api_key_returns_401(client):
    response = client.get("/bookmarks")
    assert response.status_code == 401
    assert response.json()["status_code"] == 401


def test_wrong_api_key_returns_403(client):
    response = client.get("/bookmarks", headers={"X-API-Key": "nope"})
    assert response.status_code == 403
    assert "Invalid" in response.json()["detail"]


def test_create_then_get_bookmark(client):
    created = _create(client)
    assert created.status_code == 201
    body = created.json()
    assert body["bookmark_id"]
    assert body["url"] == "https://example.com/a"
    assert body["tags"] == ["python", "web"]
    assert body["created_at"] and body["updated_at"]

    fetched = client.get("/bookmarks/{0}".format(body["bookmark_id"]), headers=AUTH_HEADERS)
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Example"


def test_create_rejects_invalid_url(client):
    response = _create(client, url="ftp://example.com/file")
    assert response.status_code == 422
    assert "url" in response.json()["detail"]


def test_create_rejects_blank_title(client):
    response = _create(client, title="   ")
    assert response.status_code == 422


def test_create_rejects_too_many_tags(client):
    response = _create(client, tags=["t{0}".format(index) for index in range(25)])
    assert response.status_code == 422


def test_create_requires_payload_fields(client):
    response = client.post("/bookmarks", json={"title": "no url"}, headers=AUTH_HEADERS)
    assert response.status_code == 422
    assert response.json()["detail"] == "Request validation failed"


def test_list_and_tag_filter(client):
    first = _create(client, url="https://a.example.com", title="A", tags=["python"]).json()
    second = _create(client, url="https://b.example.com", title="B", tags=["Rust"]).json()

    listing = client.get("/bookmarks", headers=AUTH_HEADERS)
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 2
    ids = {item["bookmark_id"] for item in body["items"]}
    assert ids == {first["bookmark_id"], second["bookmark_id"]}

    filtered = client.get("/bookmarks", params={"tag": "RUST"}, headers=AUTH_HEADERS)
    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert filtered_body["tag"] == "rust"
    assert filtered_body["count"] == 1
    assert filtered_body["items"][0]["bookmark_id"] == second["bookmark_id"]


def test_list_pagination_uses_next_token(client):
    for index in range(3):
        _create(client, url="https://example.com/{0}".format(index), title="T{0}".format(index), tags=["x"])

    page_one = client.get("/bookmarks", params={"limit": 2}, headers=AUTH_HEADERS).json()
    assert page_one["count"] == 2
    assert page_one["next_token"]

    page_two = client.get(
        "/bookmarks",
        params={"limit": 2, "next_token": page_one["next_token"]},
        headers=AUTH_HEADERS,
    ).json()
    assert page_two["count"] == 1
    assert page_two["next_token"] is None


def test_list_rejects_bad_limit_and_blank_tag(client):
    assert client.get("/bookmarks", params={"limit": 0}, headers=AUTH_HEADERS).status_code == 422
    assert client.get("/bookmarks", params={"limit": 500}, headers=AUTH_HEADERS).status_code == 422
    assert client.get("/bookmarks", params={"tag": "  "}, headers=AUTH_HEADERS).status_code == 422


def test_list_rejects_invalid_next_token(client):
    response = client.get("/bookmarks", params={"next_token": "!!!not-a-cursor!!!"}, headers=AUTH_HEADERS)
    assert response.status_code == 400
    assert "next_token" in response.json()["detail"]


def test_get_unknown_bookmark_returns_404(client):
    response = client.get("/bookmarks/does-not-exist", headers=AUTH_HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "Bookmark not found"


def test_delete_bookmark(client):
    created = _create(client).json()
    bookmark_id = created["bookmark_id"]

    deleted = client.delete("/bookmarks/{0}".format(bookmark_id), headers=AUTH_HEADERS)
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "bookmark_id": bookmark_id}

    assert client.get("/bookmarks/{0}".format(bookmark_id), headers=AUTH_HEADERS).status_code == 404
    assert client.delete("/bookmarks/{0}".format(bookmark_id), headers=AUTH_HEADERS).status_code == 404


def test_delete_requires_api_key(client):
    created = _create(client).json()
    assert client.delete("/bookmarks/{0}".format(created["bookmark_id"])).status_code == 401


def test_lifespan_runs_without_touching_aws(monkeypatch):
    monkeypatch.setenv("PRELOAD_API_KEY", "false")
    app.dependency_overrides[get_repository] = lambda: InMemoryBookmarkRepository()
    app.dependency_overrides[get_api_key_provider] = lambda: StaticApiKeyProvider(TEST_API_KEY)
    with TestClient(app) as ctx_client:
        assert ctx_client.get("/health").status_code == 200
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Fake DynamoDB
# --------------------------------------------------------------------------- #


class FakeTable:
    """Very small in-process stand-in for a boto3 DynamoDB Table."""

    def __init__(self, name, key_names):
        self.name = name
        self.key_names = key_names
        self.items = {}

    def _key_of(self, item):
        return tuple(str(item.get(part, "")) for part in self.key_names)

    def put_item(self, Item):
        self.items[self._key_of(Item)] = dict(Item)
        return {}

    def get_item(self, Key):
        item = self.items.get(self._key_of(Key))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key):
        self.items.pop(self._key_of(Key), None)
        return {}

    def _ordered(self):
        return sorted(self.items.values(), key=lambda item: self._key_of(item))

    def _paginate(self, rows, limit, start_key):
        start = 0
        if start_key:
            wanted = tuple(str(start_key.get(part, "")) for part in self.key_names)
            for index, row in enumerate(rows):
                if self._key_of(row) == wanted:
                    start = index + 1
                    break
        window = limit if limit else len(rows)
        page = rows[start:start + window]
        response = {"Items": [dict(row) for row in page]}
        if page and start + window < len(rows):
            response["LastEvaluatedKey"] = {part: page[-1].get(part) for part in self.key_names}
        return response

    def scan(self, Limit=None, ExclusiveStartKey=None, **_kwargs):
        return self._paginate(self._ordered(), Limit, ExclusiveStartKey)

    def query(self, Limit=None, ExclusiveStartKey=None, **kwargs):
        wanted = kwargs.get("ExpressionAttributeValues", {}).get(":tagvalue")
        rows = [row for row in self._ordered() if row.get("tag") == wanted]
        if kwargs.get("ScanIndexForward") is False:
            rows = list(reversed(rows))
        return self._paginate(rows, Limit, ExclusiveStartKey)


class FakeDynamoClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.described = []

    def describe_table(self, TableName):
        if self.fail:
            raise RuntimeError("connection refused")
        self.described.append(TableName)
        return {"Table": {"TableName": TableName, "TableStatus": "ACTIVE"}}


class FakeDynamoResource:
    def __init__(self, fail_describe=False):
        self.tables = {
            "bookmarks": FakeTable("bookmarks", ["bookmark_id"]),
            "bookmark_tags": FakeTable("bookmark_tags", ["tag", "bookmark_id"]),
        }
        self.meta = SimpleNamespace(client=FakeDynamoClient(fail=fail_describe))

    def Table(self, name):
        return self.tables[name]


def _dynamo_repo(fail_describe=False):
    resource = FakeDynamoResource(fail_describe=fail_describe)
    repo = DynamoBookmarkRepository(
        resource=resource,
        bookmarks_table="bookmarks",
        tags_table="bookmark_tags",
    )
    return repo, resource


def _item(bookmark_id, tags, created_at):
    return {
        "bookmark_id": bookmark_id,
        "url": "https://example.com/{0}".format(bookmark_id),
        "title": "Title {0}".format(bookmark_id),
        "tags": tags,
        "created_at": created_at,
        "updated_at": created_at,
    }


def test_dynamo_repository_roundtrip():
    repo, resource = _dynamo_repo()
    repo.create(_item("b1", ["python", "web"], "2024-01-01T00:00:00Z"))
    repo.create(_item("b2", ["rust"], "2024-01-02T00:00:00Z"))

    assert repo.get("b1")["title"] == "Title b1"
    assert repo.get("missing") is None
    assert len(resource.tables["bookmark_tags"].items) == 3

    items, token = repo.list_bookmarks(10)
    assert [item["bookmark_id"] for item in items] == ["b2", "b1"]
    assert token is None

    tagged, tag_token = repo.list_by_tag("python", 10)
    assert [item["bookmark_id"] for item in tagged] == ["b1"]
    assert tag_token is None

    removed = repo.delete("b1")
    assert removed["bookmark_id"] == "b1"
    assert repo.get("b1") is None
    assert repo.delete("b1") is None
    assert len(resource.tables["bookmark_tags"].items) == 1


def test_dynamo_repository_pagination_and_health():
    repo, _ = _dynamo_repo()
    for index in range(3):
        repo.create(_item("b{0}".format(index), ["x"], "2024-01-0{0}T00:00:00Z".format(index + 1)))

    page_one, token = repo.list_bookmarks(2)
    assert len(page_one) == 2
    assert token

    page_two, token_two = repo.list_bookmarks(2, token)
    assert len(page_two) == 1
    assert token_two is None

    assert repo.health_check() is True


def test_dynamo_repository_health_failure():
    repo, _ = _dynamo_repo(fail_describe=True)
    assert repo.health_check() is False


def test_dynamo_tag_query_falls_back_to_index_entry():
    repo, resource = _dynamo_repo()
    resource.tables["bookmark_tags"].put_item(
        Item={
            "tag": "orphan",
            "bookmark_id": "ghost",
            "url": "https://example.com/ghost",
            "title": "Ghost",
            "created_at": "2024-01-01T00:00:00Z",
        }
    )
    items, _ = repo.list_by_tag("orphan", 10)
    assert items[0]["bookmark_id"] == "ghost"
    assert items[0]["tags"] == ["orphan"]


# --------------------------------------------------------------------------- #
# Secrets Manager
# --------------------------------------------------------------------------- #


class FakeSecretsClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        self.last_id = SecretId
        if self.error is not None:
            raise self.error
        return {"SecretString": self.payload}


def test_secrets_provider_reads_json_payload_and_caches():
    fake = FakeSecretsClient(json.dumps({"api_key": "from-json"}))
    provider = SecretsManagerApiKeyProvider(client=fake, store_id="bookmark-manager/api-key")

    assert provider.get_api_key() == "from-json"
    assert provider.get_api_key() == "from-json"
    assert fake.calls == 1

    fake.payload = json.dumps({"api_key": "rotated"})
    assert provider.get_api_key(force_refresh=True) == "rotated"
    assert fake.calls == 2


def test_secrets_provider_accepts_plain_string():
    fake = FakeSecretsClient("plain-value")
    provider = SecretsManagerApiKeyProvider(client=fake, store_id="x")
    assert provider.get_api_key() == "plain-value"


def test_secrets_provider_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("API_KEY", "env-value")
    provider = SecretsManagerApiKeyProvider(
        client=FakeSecretsClient(error=RuntimeError("no secretsmanager")),
        store_id="x",
    )
    assert provider.get_api_key() == "env-value"


def test_secrets_provider_returns_none_without_fallback(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    provider = SecretsManagerApiKeyProvider(client=FakeSecretsClient("{}"), store_id="x")
    assert provider.get_api_key() is None


def test_parse_api_key_payload_variants():
    assert parse_api_key_payload(json.dumps({"apiKey": "a"})) == "a"
    assert parse_api_key_payload(json.dumps({"value": "b"})) == "b"
    assert parse_api_key_payload("  raw  ") == "raw"
    assert parse_api_key_payload("") is None
    assert parse_api_key_payload(json.dumps([1, 2])) is None


# --------------------------------------------------------------------------- #
# Configuration helpers
# --------------------------------------------------------------------------- #


def test_env_helpers(monkeypatch):
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("BOOKMARKS_TABLE", raising=False)
    monkeypatch.delenv("BOOKMARK_TAGS_TABLE", raising=False)
    monkeypatch.delenv("API_KEY_SECRET_ID", raising=False)

    assert storage.endpoint_url() is None
    assert storage.region_name() == "us-east-1"
    assert storage.bookmarks_table_name() == "bookmarks"
    assert storage.tags_table_name() == "bookmark_tags"
    assert storage.api_key_store_id() == "bookmark-manager/api-key"

    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("BOOKMARKS_TABLE", "custom")
    assert storage.endpoint_url() == "http://localhost:4566"
    assert storage.bookmarks_table_name() == "custom"


def test_clients_honour_endpoint_and_region(monkeypatch):
    captured = {}

    def fake_resource(service, **kwargs):
        captured["resource"] = dict(kwargs, service=service)
        return SimpleNamespace(Table=lambda name: FakeTable(name, ["bookmark_id"]))

    def fake_client(service, **kwargs):
        captured["client"] = dict(kwargs, service=service)
        return object()

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setattr(storage.boto3, "client", fake_client)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localstack:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    storage.dynamodb_resource()
    storage.secretsmanager_client()

    assert captured["resource"]["service"] == "dynamodb"
    assert captured["resource"]["endpoint_url"] == "http://localstack:4566"
    assert captured["resource"]["region_name"] == "us-east-1"
    assert captured["client"]["service"] == "secretsmanager"
    assert captured["client"]["endpoint_url"] == "http://localstack:4566"


def test_token_helpers_roundtrip_and_errors():
    token = encode_token({"offset": 5})
    assert decode_token(token) == {"offset": 5}
    assert encode_token(None) is None
    assert decode_token(None) is None
    with pytest.raises(TokenError):
        decode_token("###")
    with pytest.raises(TokenError):
        decode_token(encode_token({"offset": 1}).replace("e", "z"))


def test_in_memory_repository_rejects_bad_offset():
    repo = InMemoryBookmarkRepository([_item("b1", ["x"], "2024-01-01T00:00:00Z")])
    bad_token = encode_token({"offset": "not-a-number"})
    with pytest.raises(TokenError):
        repo.list_bookmarks(10, bad_token)
