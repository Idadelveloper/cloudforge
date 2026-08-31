"""Offline tests for the URL shortener API (no AWS or network access)."""
import os
import sys
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage  # noqa: E402
from app import app, get_repository  # noqa: E402


class FakeRepository(storage.InMemoryUrlRepository):
    """In-memory repository with a controllable health check."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_health = False

    def health(self) -> str:
        if self.fail_health:
            raise RuntimeError("table unreachable")
        return "ACTIVE"


@pytest.fixture()
def repo() -> FakeRepository:
    return FakeRepository()


@pytest.fixture()
def client(repo: FakeRepository) -> TestClient:
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def get_no_redirect(client: TestClient, url: str):
    try:
        return client.get(url, follow_redirects=False)
    except TypeError:  # pragma: no cover - older TestClient API
        return client.get(url, allow_redirects=False)


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["table_status"] == "ACTIVE"
    assert body["service"] == "url_shortener_api"


def test_health_degraded(client: TestClient, repo: FakeRepository) -> None:
    repo.fail_health = True
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["table_status"] == "unavailable"


def test_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "url_shortener_api"


def test_shorten_redirect_and_stats(client: TestClient) -> None:
    created = client.post("/shorten", json={"url": "https://example.com/a/very/long/path?x=1"})
    assert created.status_code == 201
    body = created.json()
    code = body["code"]
    assert body["long_url"] == "https://example.com/a/very/long/path?x=1"
    assert body["short_url"].endswith("/" + code)
    assert body["created_at"]

    first = get_no_redirect(client, "/" + code)
    assert first.status_code == 307
    assert first.headers["location"] == "https://example.com/a/very/long/path?x=1"
    second = get_no_redirect(client, "/" + code)
    assert second.status_code == 307

    stats = client.get("/api/stats/" + code)
    assert stats.status_code == 200
    payload = stats.json()
    assert payload["code"] == code
    assert payload["visit_count"] == 2
    assert payload["long_url"] == "https://example.com/a/very/long/path?x=1"
    assert payload["last_visited_at"] is not None


def test_shorten_with_custom_code_and_conflict(client: TestClient) -> None:
    response = client.post("/shorten", json={"url": "http://example.org", "custom_code": "my-link_1"})
    assert response.status_code == 201
    assert response.json()["code"] == "my-link_1"

    duplicate = client.post("/shorten", json={"url": "http://example.org/other", "custom_code": "my-link_1"})
    assert duplicate.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"url": ""},
        {"url": "not-a-url"},
        {"url": "ftp://example.com/file"},
        {"url": "https://example.com", "custom_code": "ab"},
        {"url": "https://example.com", "custom_code": "bad code!"},
        {"url": "https://example.com", "custom_code": "health"},
    ],
)
def test_shorten_validation_errors(client: TestClient, payload: Dict[str, Any]) -> None:
    response = client.post("/shorten", json=payload)
    assert response.status_code == 422


def test_shorten_url_too_long(client: TestClient) -> None:
    long_url = "https://example.com/" + ("a" * 2100)
    response = client.post("/shorten", json={"url": long_url})
    assert response.status_code == 422


def test_shorten_retries_on_collision(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    first = client.post("/shorten", json={"url": "https://example.com/one", "custom_code": "dupdup"})
    assert first.status_code == 201

    candidates = iter(["dupdup", "health", "freshcode"])
    monkeypatch.setattr(app_module, "_generate_code", lambda: next(candidates))
    second = client.post("/shorten", json={"url": "https://example.com/two"})
    assert second.status_code == 201
    assert second.json()["code"] == "freshcode"


def test_shorten_exhausts_attempts(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "_generate_code", lambda: "health")
    response = client.post("/shorten", json={"url": "https://example.com/x"})
    assert response.status_code == 503


def test_redirect_unknown_code(client: TestClient) -> None:
    assert get_no_redirect(client, "/nosuchcode").status_code == 404
    assert get_no_redirect(client, "/xy").status_code == 404


def test_stats_unknown_code(client: TestClient) -> None:
    assert client.get("/api/stats/nosuchcode").status_code == 404
    assert client.get("/api/stats/xy").status_code == 404


def test_list_links_pagination(client: TestClient) -> None:
    for index in range(3):
        payload = {"url": "https://example.com/%d" % index, "custom_code": "code%d" % index}
        assert client.post("/shorten", json=payload).status_code == 201

    page = client.get("/api/links", params={"limit": 2})
    assert page.status_code == 200
    body = page.json()
    assert body["count"] == 2
    assert body["next"] == "code1"
    assert body["items"][0]["code"] == "code0"
    assert body["items"][0]["visit_count"] == 0

    rest = client.get("/api/links", params={"limit": 2, "start": body["next"]})
    assert rest.status_code == 200
    rest_body = rest.json()
    assert [item["code"] for item in rest_body["items"]] == ["code2"]
    assert rest_body["next"] is None


def test_list_links_rejects_bad_limit(client: TestClient) -> None:
    assert client.get("/api/links", params={"limit": 0}).status_code == 422


def test_delete_link(client: TestClient) -> None:
    created = client.post("/shorten", json={"url": "https://example.com/gone", "custom_code": "deleteme"})
    assert created.status_code == 201

    deleted = client.delete("/api/links/deleteme")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "code": "deleteme"}

    assert client.delete("/api/links/deleteme").status_code == 404
    assert client.delete("/api/links/xy").status_code == 404
    assert get_no_redirect(client, "/deleteme").status_code == 404


class BrokenRepository(storage.UrlRepository):
    """Repository whose operations always fail, exercising the 503 path."""

    def create(self, item: Dict[str, Any]) -> bool:
        raise RuntimeError("boom")

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        raise RuntimeError("boom")

    def increment_visit(self, code: str, timestamp: str) -> Optional[Dict[str, Any]]:
        raise RuntimeError("boom")

    def list_items(self, limit: int, start_code: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        raise RuntimeError("boom")

    def delete(self, code: str) -> bool:
        raise RuntimeError("boom")

    def health(self) -> str:
        raise RuntimeError("boom")


def test_storage_failures_return_503() -> None:
    app.dependency_overrides[get_repository] = BrokenRepository
    broken_client = TestClient(app)
    assert broken_client.post("/shorten", json={"url": "https://example.com"}).status_code == 503
    assert broken_client.get("/api/stats/abcdef").status_code == 503
    assert broken_client.get("/api/links").status_code == 503
    assert broken_client.delete("/api/links/abcdef").status_code == 503
    assert get_no_redirect(broken_client, "/abcdef").status_code == 503
    app.dependency_overrides.clear()


class ConditionalCheckFailed(Exception):
    pass


class FakeExceptions:
    ConditionalCheckFailedException = ConditionalCheckFailed


class FakeClient:
    exceptions = FakeExceptions()

    def __init__(self, status: str = "ACTIVE") -> None:
        self.status = status
        self.described: List[str] = []

    def describe_table(self, TableName: str) -> Dict[str, Any]:  # noqa: N803 - boto3 kwarg name
        self.described.append(TableName)
        return {"Table": {"TableStatus": self.status}}


class FakeMeta:
    def __init__(self, client: FakeClient) -> None:
        self.client = client


class FakeTable:
    """Minimal stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []
        self.meta = FakeMeta(FakeClient())

    def put_item(self, Item: Dict[str, Any], ConditionExpression: str = "") -> Dict[str, Any]:  # noqa: N803
        code = str(Item["code"])
        if "attribute_not_exists" in ConditionExpression and code in self.items:
            raise ConditionalCheckFailed("exists")
        if code not in self.items:
            self.order.append(code)
        self.items[code] = dict(Item)
        return {}

    def get_item(self, Key: Dict[str, Any]) -> Dict[str, Any]:  # noqa: N803
        item = self.items.get(str(Key["code"]))
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs: Any) -> Dict[str, Any]:
        code = str(kwargs["Key"]["code"])
        values = kwargs.get("ExpressionAttributeValues", {})
        if code not in self.items:
            raise ConditionalCheckFailed("missing")
        item = self.items[code]
        current = item.get("visit_count") or 0
        item["visit_count"] = Decimal(int(current) + int(values[":one"]))
        item["last_visited_at"] = values[":ts"]
        return {"Attributes": dict(item)}

    def delete_item(self, Key: Dict[str, Any], ConditionExpression: str = "") -> Dict[str, Any]:  # noqa: N803
        code = str(Key["code"])
        if code not in self.items:
            raise ConditionalCheckFailed("missing")
        del self.items[code]
        self.order.remove(code)
        return {}

    def scan(self, **kwargs: Any) -> Dict[str, Any]:
        codes = list(self.order)
        start = kwargs.get("ExclusiveStartKey", {}).get("code")
        if start in codes:
            codes = codes[codes.index(start) + 1:]
        limit = int(kwargs.get("Limit", len(codes) or 1))
        page = codes[:limit]
        response: Dict[str, Any] = {"Items": [dict(self.items[code]) for code in page]}
        if page and len(codes) > len(page):
            response["LastEvaluatedKey"] = {"code": page[-1]}
        return response


def test_dynamo_repository_against_fake_table() -> None:
    table = FakeTable()
    repository = storage.DynamoUrlRepository(name="unit_table", table=table)

    item = {"code": "abc123", "long_url": "https://example.com", "created_at": "2024-01-01T00:00:00Z"}
    assert repository.create(item) is True
    assert repository.create(item) is False

    fetched = repository.get("abc123")
    assert fetched is not None
    assert fetched["visit_count"] == 0
    assert fetched["last_visited_at"] is None
    assert repository.get("missing") is None

    updated = repository.increment_visit("abc123", "2024-01-02T00:00:00Z")
    assert updated is not None
    assert updated["visit_count"] == 1
    assert isinstance(updated["visit_count"], int)
    assert repository.increment_visit("missing", "2024-01-02T00:00:00Z") is None

    second = {"code": "def456", "long_url": "https://example.net", "created_at": "2024-01-03T00:00:00Z"}
    assert repository.create(second) is True
    page, next_key = repository.list_items(1)
    assert [entry["code"] for entry in page] == ["abc123"]
    assert next_key == "abc123"
    rest, rest_next = repository.list_items(10, next_key)
    assert [entry["code"] for entry in rest] == ["def456"]
    assert rest_next is None

    assert repository.health() == "ACTIVE"
    assert table.meta.client.described == ["unit_table"]

    assert repository.delete("abc123") is True
    assert repository.delete("abc123") is False


def test_dynamodb_resource_uses_endpoint_and_region(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    class FakeResource:
        def Table(self, name: str) -> str:  # noqa: N802 - boto3 API name
            captured["table"] = name
            return "table:" + name

    def fake_resource(service: str, **kwargs: Any) -> FakeResource:
        captured["service"] = service
        captured.update(kwargs)
        return FakeResource()

    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("MAPPINGS_TABLE_NAME", "custom_table")
    monkeypatch.setattr(storage.boto3, "resource", fake_resource)

    assert storage.table_name() == "custom_table"
    repository = storage.DynamoUrlRepository()
    assert repository.table == "table:custom_table"
    assert captured["service"] == "dynamodb"
    assert captured["region_name"] == "us-east-1"
    assert captured["endpoint_url"] == "http://localhost:4566"


def test_dynamodb_resource_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("MAPPINGS_TABLE_NAME", raising=False)
    monkeypatch.delenv("MAPPINGS_TABLE", raising=False)
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)

    assert storage.aws_endpoint_url() is None
    assert storage.aws_region() == "us-east-1"
    assert storage.table_name() == "url_shortener_mappings"


def test_normalize_item_handles_decimals() -> None:
    normalized = storage.normalize_item({"code": "a", "visit_count": Decimal("5"), "ratio": Decimal("1.5")})
    assert normalized["visit_count"] == 5
    assert normalized["ratio"] == 1.5
    assert normalized["last_visited_at"] is None


def test_repository_interface_is_abstract() -> None:
    base = storage.UrlRepository()
    for call in (
        lambda: base.create({}),
        lambda: base.get("a"),
        lambda: base.increment_visit("a", "t"),
        lambda: base.list_items(1),
        lambda: base.delete("a"),
        base.health,
    ):
        with pytest.raises(NotImplementedError):
            call()


def test_get_repository_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "_repository", None)
    first = app_module.get_repository()
    second = app_module.get_repository()
    assert first is second
    assert isinstance(first, storage.DynamoUrlRepository)
    monkeypatch.setattr(app_module, "_repository", None)


def test_public_base_url_override(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://sho.rt/")
    response = client.post("/shorten", json={"url": "https://example.com/base"})
    assert response.status_code == 201
    assert response.json()["short_url"].startswith("https://sho.rt/")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


def test_int_env_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_INT", "12")
    assert app_module._int_env("SOME_INT", 5) == 12
    monkeypatch.setenv("SOME_INT", "not-a-number")
    assert app_module._int_env("SOME_INT", 5) == 5
    monkeypatch.delenv("SOME_INT", raising=False)
    assert app_module._int_env("SOME_INT", 7) == 7
