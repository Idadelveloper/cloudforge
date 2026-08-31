"""Offline tests for the url_shortener API and its storage layer."""

import pytest
from fastapi.testclient import TestClient

import app as app_module
import storage
from storage import (
    CodeAlreadyExistsError,
    DynamoUrlRepository,
    InMemoryUrlRepository,
    NotFoundError,
    StorageError,
)


@pytest.fixture
def repo():
    return InMemoryUrlRepository()


@pytest.fixture
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _get_no_redirect(client, path):
    try:
        return client.get(path, follow_redirects=False)
    except TypeError:  # pragma: no cover - older test clients
        return client.get(path, allow_redirects=False)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["table_reachable"] is True
    assert body["table"]


def test_create_and_stats(client):
    response = client.post("/urls", json={"url": "https://example.com/a/very/long/path?x=1"})
    assert response.status_code == 201
    body = response.json()
    code = body["code"]
    assert code
    assert body["long_url"] == "https://example.com/a/very/long/path?x=1"
    assert body["short_url"].endswith("/" + code)
    assert body["visit_count"] == 0

    stats = client.get("/urls/{0}/stats".format(code))
    assert stats.status_code == 200
    stats_body = stats.json()
    assert stats_body["code"] == code
    assert stats_body["visit_count"] == 0
    assert stats_body["last_visited_at"] is None
    assert stats_body["created_at"]


def test_create_with_custom_code_and_conflict(client):
    response = client.post("/urls", json={"url": "https://example.org", "custom_code": "my-code_1"})
    assert response.status_code == 201
    assert response.json()["code"] == "my-code_1"

    conflict = client.post("/urls", json={"url": "https://example.net", "custom_code": "my-code_1"})
    assert conflict.status_code == 409
    assert "already exists" in conflict.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"url": ""},
        {"url": "ftp://example.com/file"},
        {"url": "https://"},
        {"url": "https://example.com", "custom_code": "ab"},
        {"url": "https://example.com", "custom_code": "bad code!"},
        {"url": "https://example.com", "custom_code": "health"},
    ],
)
def test_create_rejects_bad_input(client, payload):
    response = client.post("/urls", json=payload)
    assert response.status_code == 400
    assert "detail" in response.json()


def test_create_rejects_missing_body(client):
    response = client.post("/urls", json={})
    assert response.status_code == 422


def test_redirect_increments_counter(client):
    created = client.post("/urls", json={"url": "https://example.com/target", "custom_code": "go-here"})
    assert created.status_code == 201

    for expected in (1, 2, 3):
        response = _get_no_redirect(client, "/go-here")
        assert response.status_code == 307
        assert response.headers["location"] == "https://example.com/target"
        stats = client.get("/urls/go-here/stats").json()
        assert stats["visit_count"] == expected
        assert stats["last_visited_at"]


def test_redirect_unknown_code(client):
    response = _get_no_redirect(client, "/does-not-exist")
    assert response.status_code == 404
    assert "unknown code" in response.json()["detail"]


def test_stats_unknown_code(client):
    response = client.get("/urls/nope/stats")
    assert response.status_code == 404


def test_list_urls_with_pagination(client):
    for index in range(3):
        created = client.post(
            "/urls",
            json={"url": "https://example.com/{0}".format(index), "custom_code": "code{0}".format(index)},
        )
        assert created.status_code == 201

    first = client.get("/urls", params={"limit": 2}).json()
    assert first["count"] == 2
    assert [item["code"] for item in first["items"]] == ["code0", "code1"]
    assert first["next_code"] == "code1"

    second = client.get("/urls", params={"limit": 2, "start_after": first["next_code"]}).json()
    assert [item["code"] for item in second["items"]] == ["code2"]
    assert second["next_code"] is None


def test_list_urls_validates_limit(client):
    assert client.get("/urls", params={"limit": 0}).status_code == 422
    assert client.get("/urls", params={"limit": 500}).status_code == 422


def test_delete_url(client):
    client.post("/urls", json={"url": "https://example.com", "custom_code": "temp1"})
    assert client.delete("/urls/temp1").status_code == 204
    assert client.get("/urls/temp1/stats").status_code == 404
    assert _get_no_redirect(client, "/temp1").status_code == 404
    assert client.delete("/urls/temp1").status_code == 404


def test_storage_unavailable_returns_503(client):
    class BrokenRepo(InMemoryUrlRepository):
        def create(self, code, long_url, created_at):
            raise StorageError("boom")

        def get(self, code):
            raise StorageError("boom")

        def list_urls(self, limit=25, start_after=None):
            raise StorageError("boom")

        def register_visit(self, code, visited_at):
            raise StorageError("boom")

        def delete(self, code):
            raise StorageError("boom")

        def healthy(self):
            raise StorageError("boom")

    app_module.app.dependency_overrides[app_module.get_repository] = BrokenRepo
    assert client.post("/urls", json={"url": "https://example.com"}).status_code == 503
    assert client.get("/urls").status_code == 503
    assert client.get("/urls/x/stats").status_code == 503
    assert client.delete("/urls/x").status_code == 503
    assert _get_no_redirect(client, "/x").status_code == 503
    assert client.get("/health").json()["table_reachable"] is False


# --------------------------------------------------------------------------
# Storage layer tests using a fake DynamoDB table (no network / LocalStack)
# --------------------------------------------------------------------------


class FakeClientError(Exception):
    pass


class FakeConditionalCheckFailed(FakeClientError):
    pass


class FakeExceptions:
    ClientError = FakeClientError
    ConditionalCheckFailedException = FakeConditionalCheckFailed


class FakeClient:
    def __init__(self, table):
        self._table = table
        self.exceptions = FakeExceptions()

    def describe_table(self, TableName):
        if self._table.fail:
            raise FakeClientError("no such table")
        return {"Table": {"TableName": TableName, "TableStatus": "ACTIVE"}}


class FakeMeta:
    def __init__(self, client):
        self.client = client


class FakeTable:
    def __init__(self, fail=False):
        self.items = {}
        self.fail = fail
        self.meta = FakeMeta(FakeClient(self))

    def _guard(self):
        if self.fail:
            raise FakeClientError("table unavailable")

    def put_item(self, Item, ConditionExpression=None):
        self._guard()
        if ConditionExpression and "attribute_not_exists" in ConditionExpression:
            if Item["code"] in self.items:
                raise FakeConditionalCheckFailed("exists")
        self.items[Item["code"]] = dict(Item)
        return {}

    def get_item(self, Key):
        self._guard()
        item = self.items.get(Key["code"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs):
        self._guard()
        code = kwargs["Key"]["code"]
        values = kwargs.get("ExpressionAttributeValues") or {}
        item = self.items.get(code)
        if item is None:
            raise FakeConditionalCheckFailed("missing")
        item["visit_count"] = int(item.get("visit_count") or 0) + int(values[":inc"])
        item["last_visited_at"] = values[":ts"]
        return {"Attributes": dict(item)}

    def delete_item(self, Key, ConditionExpression=None):
        self._guard()
        if Key["code"] not in self.items:
            raise FakeConditionalCheckFailed("missing")
        del self.items[Key["code"]]
        return {}

    def scan(self, **kwargs):
        self._guard()
        codes = sorted(self.items)
        start = kwargs.get("ExclusiveStartKey")
        if start:
            codes = [code for code in codes if code > start["code"]]
        limit = int(kwargs.get("Limit", len(codes) or 1))
        page = codes[:limit]
        response = {"Items": [dict(self.items[code]) for code in page]}
        if len(codes) > limit and page:
            response["LastEvaluatedKey"] = {"code": page[-1]}
        return response


def test_dynamo_repository_round_trip():
    table = FakeTable()
    repo = DynamoUrlRepository(table=table, table_name_override="unit-table")

    created = repo.create("abc123", "https://example.com", "2024-01-01T00:00:00Z")
    assert created["code"] == "abc123"
    assert created["visit_count"] == 0

    with pytest.raises(CodeAlreadyExistsError):
        repo.create("abc123", "https://other.example", "2024-01-01T00:00:00Z")

    fetched = repo.get("abc123")
    assert fetched is not None and fetched["long_url"] == "https://example.com"
    assert repo.get("missing") is None

    visited = repo.register_visit("abc123", "2024-01-02T00:00:00Z")
    assert visited["visit_count"] == 1
    assert visited["last_visited_at"] == "2024-01-02T00:00:00Z"

    with pytest.raises(NotFoundError):
        repo.register_visit("missing", "2024-01-02T00:00:00Z")

    repo.create("zzz999", "https://example.com/2", "2024-01-01T00:00:00Z")
    items, next_code = repo.list_urls(limit=1)
    assert [item["code"] for item in items] == ["abc123"]
    assert next_code == "abc123"
    items, next_code = repo.list_urls(limit=1, start_after="abc123")
    assert [item["code"] for item in items] == ["zzz999"]
    assert next_code is None

    assert repo.healthy() is True
    repo.delete("abc123")
    assert repo.get("abc123") is None
    with pytest.raises(NotFoundError):
        repo.delete("abc123")


def test_dynamo_repository_wraps_client_errors():
    repo = DynamoUrlRepository(table=FakeTable(fail=True), table_name_override="unit-table")
    with pytest.raises(StorageError):
        repo.create("abc", "https://example.com", "2024-01-01T00:00:00Z")
    with pytest.raises(StorageError):
        repo.get("abc")
    with pytest.raises(StorageError):
        repo.list_urls()
    assert repo.healthy() is False


def test_dynamodb_resource_uses_endpoint_env(monkeypatch):
    captured = {}

    class FakeBoto3:
        @staticmethod
        def resource(name, **kwargs):
            captured["name"] = name
            captured.update(kwargs)
            return "fake-resource"

    monkeypatch.setattr(storage, "boto3", FakeBoto3)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    assert storage.dynamodb_resource() == "fake-resource"
    assert captured["name"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "us-east-1"


def test_table_name_default(monkeypatch):
    monkeypatch.delenv("URL_TABLE_NAME", raising=False)
    assert storage.table_name() == "url_shortener_urls"
    monkeypatch.setenv("URL_TABLE_NAME", "custom_table")
    assert storage.table_name() == "custom_table"


def test_code_generation_helpers(monkeypatch):
    monkeypatch.setenv("SHORT_CODE_LENGTH", "10")
    assert len(app_module.generate_code()) == 10
    monkeypatch.setenv("SHORT_CODE_LENGTH", "not-a-number")
    assert len(app_module.generate_code()) == 7
    monkeypatch.setenv("SHORT_URL_BASE_URL", "https://sho.rt/")
    assert app_module.build_short_url("abc") == "https://sho.rt/abc"
