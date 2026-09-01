"""Offline tests for the contact-form backend (no AWS/network required)."""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module  # noqa: E402
import storage as storage_module  # noqa: E402

ADMIN_HEADER_VALUE = "unit-test-admin-credential"
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_HEADER_VALUE}


class FakeRepository(storage_module.MessageRepository):
    """In-memory repository injected in place of DynamoDB."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.reachable = True

    def create_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        self.items[str(item["id"])] = dict(item)
        return dict(item)

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        item = self.items.get(message_id)
        return dict(item) if item else None

    def list_messages(
        self,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        ordered = sorted(
            self.items.values(),
            key=lambda entry: str(entry.get("created_at", "")),
            reverse=True,
        )
        offset = 0
        if next_token:
            offset = int(storage_module.decode_token(next_token).get("offset", 0))
        page = ordered[offset:offset + limit]
        token = None
        if offset + limit < len(ordered):
            token = storage_module.encode_token({"offset": offset + limit})
        return [dict(entry) for entry in page], token

    def delete_message(self, message_id: str) -> bool:
        return self.items.pop(message_id, None) is not None

    def healthy(self) -> bool:
        return self.reachable


class FakeTable:
    """Minimal stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.load_calls = 0
        self.fail_load = False

    def load(self) -> None:
        self.load_calls += 1
        if self.fail_load:
            raise RuntimeError("table not found")

    def put_item(self, Item: Dict[str, Any]) -> Dict[str, Any]:
        self.items[str(Item["id"])] = dict(Item)
        return {}

    def get_item(self, Key: Dict[str, Any]) -> Dict[str, Any]:
        item = self.items.get(str(Key["id"]))
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs: Any) -> Dict[str, Any]:
        limit = int(kwargs.get("Limit", 100))
        keys = sorted(self.items)
        start_key = kwargs.get("ExclusiveStartKey")
        if start_key:
            keys = [key for key in keys if key > str(start_key["id"])]
        page = keys[:limit]
        result: Dict[str, Any] = {"Items": [dict(self.items[key]) for key in page]}
        if page and len(keys) > len(page):
            result["LastEvaluatedKey"] = {"id": page[-1]}
        return result

    def delete_item(self, Key: Dict[str, Any], ReturnValues: Optional[str] = None) -> Dict[str, Any]:
        item = self.items.pop(str(Key["id"]), None)
        return {"Attributes": item} if item else {}


@pytest.fixture()
def repo() -> FakeRepository:
    return FakeRepository()


@pytest.fixture()
def client(repo: FakeRepository, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_HEADER_VALUE)
    monkeypatch.setenv("TABLE_NAME", "test-contact-messages")
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _submit(test_client: TestClient, name: str = "Ada", email: str = "ada@example.com",
            message: str = "Hello there") -> Any:
    return test_client.post(
        "/messages",
        json={"name": name, "email": email, "message": message},
    )


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #
def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["table"] == "test-contact-messages"
    assert body["table_reachable"] is True


def test_health_degraded_when_table_unreachable(client: TestClient, repo: FakeRepository) -> None:
    repo.reachable = False
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["table_reachable"] is False


# --------------------------------------------------------------------------- #
# POST /messages
# --------------------------------------------------------------------------- #
def test_create_message_success(client: TestClient, repo: FakeRepository) -> None:
    response = _submit(client)
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Ada"
    assert body["email"] == "ada@example.com"
    assert body["message"] == "Hello there"
    assert body["id"]
    assert body["created_at"].endswith("Z")
    assert body["id"] in repo.items


def test_create_message_trims_whitespace(client: TestClient) -> None:
    response = _submit(client, name="  Grace  ", email=" grace@example.com ", message="  hi  ")
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Grace"
    assert body["email"] == "grace@example.com"
    assert body["message"] == "hi"


def test_create_message_rejects_invalid_email(client: TestClient) -> None:
    response = _submit(client, email="not-an-email")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert "email" in body["detail"]


def test_create_message_rejects_blank_name(client: TestClient) -> None:
    response = _submit(client, name="   ")
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_create_message_requires_all_fields(client: TestClient) -> None:
    response = client.post("/messages", json={"name": "Ada"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_create_message_rejects_oversized_body(client: TestClient) -> None:
    response = _submit(client, message="x" * 5001)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --------------------------------------------------------------------------- #
# GET /messages
# --------------------------------------------------------------------------- #
def test_list_requires_admin_token(client: TestClient) -> None:
    response = client.get("/messages")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_list_rejects_wrong_admin_token(client: TestClient) -> None:
    response = client.get("/messages", headers={"X-Admin-Token": "nope"})
    assert response.status_code == 401


def test_list_messages_newest_first_with_pagination(client: TestClient, repo: FakeRepository) -> None:
    for index in range(3):
        repo.create_message(
            {
                "id": "id-%d" % index,
                "name": "Visitor %d" % index,
                "email": "v%d@example.com" % index,
                "message": "msg %d" % index,
                "created_at": "2024-01-0%dT00:00:00Z" % (index + 1),
                "source_ip": None,
            }
        )

    first = client.get("/messages", params={"limit": 2}, headers=ADMIN_HEADERS)
    assert first.status_code == 200
    payload = first.json()
    assert payload["count"] == 2
    assert [item["id"] for item in payload["items"]] == ["id-2", "id-1"]
    assert payload["next_token"]

    second = client.get(
        "/messages",
        params={"limit": 2, "next_token": payload["next_token"]},
        headers=ADMIN_HEADERS,
    )
    assert second.status_code == 200
    tail = second.json()
    assert [item["id"] for item in tail["items"]] == ["id-0"]
    assert tail["next_token"] is None


def test_list_messages_invalid_limit(client: TestClient) -> None:
    response = client.get("/messages", params={"limit": 0}, headers=ADMIN_HEADERS)
    assert response.status_code == 422


def test_list_messages_invalid_token(client: TestClient) -> None:
    response = client.get("/messages", params={"next_token": "!!!"}, headers=ADMIN_HEADERS)
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# GET/DELETE /messages/{id}
# --------------------------------------------------------------------------- #
def test_get_single_message(client: TestClient) -> None:
    created = _submit(client).json()
    response = client.get("/messages/%s" % created["id"], headers=ADMIN_HEADERS)
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_single_message_requires_admin(client: TestClient) -> None:
    created = _submit(client).json()
    assert client.get("/messages/%s" % created["id"]).status_code == 401


def test_get_missing_message_returns_404(client: TestClient) -> None:
    response = client.get("/messages/does-not-exist", headers=ADMIN_HEADERS)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_delete_message_flow(client: TestClient, repo: FakeRepository) -> None:
    created = _submit(client).json()
    assert client.delete("/messages/%s" % created["id"]).status_code == 401

    deleted = client.delete("/messages/%s" % created["id"], headers=ADMIN_HEADERS)
    assert deleted.status_code == 204
    assert created["id"] not in repo.items

    again = client.delete("/messages/%s" % created["id"], headers=ADMIN_HEADERS)
    assert again.status_code == 404


# --------------------------------------------------------------------------- #
# storage layer
# --------------------------------------------------------------------------- #
def test_dynamodb_repository_crud_against_fake_table() -> None:
    table = FakeTable()
    repo = storage_module.DynamoDBMessageRepository(table_name="t", table=table)

    repo.create_message(
        {
            "id": "a",
            "name": "A",
            "email": "a@example.com",
            "message": "first",
            "created_at": "2024-01-01T00:00:00Z",
            "source_ip": None,
        }
    )
    repo.create_message(
        {
            "id": "b",
            "name": "B",
            "email": "b@example.com",
            "message": "second",
            "created_at": "2024-02-01T00:00:00Z",
            "source_ip": "1.2.3.4",
        }
    )

    assert repo.table_name == "t"
    assert repo.get_message("a")["name"] == "A"
    assert repo.get_message("missing") is None

    items, token = repo.list_messages(limit=1)
    assert len(items) == 1
    assert token is not None

    rest, next_token = repo.list_messages(limit=1, next_token=token)
    assert [item["id"] for item in rest] == ["b"]
    assert next_token is None

    assert repo.delete_message("a") is True
    assert repo.delete_message("a") is False
    assert repo.healthy() is True

    table.fail_load = True
    assert repo.healthy() is False


def test_dynamodb_repository_rejects_bad_token() -> None:
    repo = storage_module.DynamoDBMessageRepository(table_name="t", table=FakeTable())
    with pytest.raises(storage_module.InvalidPaginationToken):
        repo.list_messages(next_token="@@not-base64@@")


def test_token_round_trip() -> None:
    token = storage_module.encode_token({"id": "abc"})
    assert storage_module.decode_token(token) == {"id": "abc"}


def test_in_memory_repository_pagination() -> None:
    repo = storage_module.InMemoryMessageRepository()
    for index in range(3):
        repo.create_message(
            {
                "id": str(index),
                "name": "n",
                "email": "n@example.com",
                "message": "m",
                "created_at": "2024-01-0%dT00:00:00Z" % (index + 1),
            }
        )
    page, token = repo.list_messages(limit=2)
    assert [item["id"] for item in page] == ["2", "1"]
    tail, next_token = repo.list_messages(limit=2, next_token=token)
    assert [item["id"] for item in tail] == ["0"]
    assert next_token is None
    assert repo.healthy() is True


def test_dynamodb_resource_uses_env_endpoint_and_region(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def fake_resource(service_name: str, **kwargs: Any) -> str:
        captured["service_name"] = service_name
        captured.update(kwargs)
        return "fake-resource"

    monkeypatch.setattr(storage_module.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    assert storage_module.dynamodb_resource() == "fake-resource"
    assert captured["service_name"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "eu-west-1"


def test_defaults_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABLE_NAME", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert storage_module.table_name() == "contact-messages"
    assert storage_module.aws_region() == "us-east-1"


def test_get_repository_is_cached_dynamodb_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "_repository", None, raising=False)
    monkeypatch.setenv("TABLE_NAME", "env-table")
    first = app_module.get_repository()
    second = app_module.get_repository()
    assert isinstance(first, storage_module.DynamoDBMessageRepository)
    assert first is second
    assert first.table_name == "env-table"
    monkeypatch.setattr(app_module, "_repository", None, raising=False)
