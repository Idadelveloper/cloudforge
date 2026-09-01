"""Offline tests for the shop inventory API (no AWS or network access)."""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage  # noqa: E402


class ConditionalCheckFailed(Exception):
    """Mimics botocore's ConditionalCheckFailedException shape."""

    def __init__(self) -> None:
        super().__init__("the conditional request failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """Minimal in-process stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, fail_load=False):
        self.items = {}
        self.fail_load = fail_load
        self.scan_calls = []
        self.last_evaluated_key = None

    def load(self):
        if self.fail_load:
            raise RuntimeError("endpoint unreachable")
        return None

    def put_item(self, Item, ConditionExpression=None):
        sku = Item["sku"]
        if ConditionExpression and sku in self.items:
            raise ConditionalCheckFailed()
        self.items[sku] = dict(Item)
        return {}

    def get_item(self, Key):
        item = self.items.get(Key["sku"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        result = {"Items": [dict(v) for v in self.items.values()]}
        if self.last_evaluated_key is not None:
            result["LastEvaluatedKey"] = self.last_evaluated_key
        return result

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
        ReturnValues=None,
    ):
        sku = Key["sku"]
        item = self.items.get(sku)
        if item is None:
            raise ConditionalCheckFailed()
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        if "ADD" in UpdateExpression:
            needed = values.get(":needed")
            current = int(item.get("quantity", 0))
            if needed is not None and current < int(needed):
                raise ConditionalCheckFailed()
            item["quantity"] = current + int(values[":d"])
        for placeholder, attribute in names.items():
            if attribute == "quantity":
                continue
            value_key = ":" + placeholder[1:]
            if value_key in values:
                item[attribute] = values[value_key]
        self.items[sku] = item
        return {"Attributes": dict(item)}


@pytest.fixture()
def repo():
    return storage.InMemoryProductRepository()


@pytest.fixture()
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


@pytest.fixture()
def dynamo_client():
    table = FakeTable()
    dynamo_repo = storage.DynamoDBProductRepository(table_name="test-products", table=table)
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: dynamo_repo
    with TestClient(app_module.app) as test_client:
        yield test_client, table
    app_module.app.dependency_overrides.clear()


def _create(client, sku="SKU-1", name="Tea", price=3.5, quantity=10):
    return client.post(
        "/products",
        json={"sku": sku, "name": name, "price": price, "quantity": quantity},
    )


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dynamodb"] == "reachable"
    assert body["table"] == "in-memory-products"
    assert body["time"].endswith("Z")


def test_create_product(client):
    response = _create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["sku"] == "SKU-1"
    assert body["name"] == "Tea"
    assert body["price"] == 3.5
    assert body["quantity"] == 10
    assert body["created_at"] and body["updated_at"]


def test_create_product_defaults_quantity_to_zero(client):
    response = client.post("/products", json={"sku": "SKU-Z", "name": "Zero", "price": 1})
    assert response.status_code == 201
    assert response.json()["quantity"] == 0


def test_create_duplicate_sku_conflicts(client):
    assert _create(client).status_code == 201
    response = _create(client)
    assert response.status_code == 409
    assert response.json()["error"] == "sku_exists"


def test_create_validation_error(client):
    response = client.post("/products", json={"sku": "", "name": "x", "price": -1})
    assert response.status_code == 422


def test_create_blank_sku_rejected(client):
    response = client.post("/products", json={"sku": "   ", "name": "x", "price": 1})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_sku"


def test_list_products_and_pagination(client):
    for index in range(3):
        assert _create(client, sku="SKU-%d" % index, name="Item %d" % index).status_code == 201

    response = client.get("/products")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert body["next_token"] is None

    page_one = client.get("/products", params={"limit": 2}).json()
    assert page_one["count"] == 2
    assert page_one["next_token"]

    page_two = client.get(
        "/products", params={"limit": 2, "next_token": page_one["next_token"]}
    ).json()
    assert page_two["count"] == 1
    assert page_two["next_token"] is None

    seen = {item["sku"] for item in page_one["items"] + page_two["items"]}
    assert seen == {"SKU-0", "SKU-1", "SKU-2"}


def test_list_products_bad_token(client):
    response = client.get("/products", params={"next_token": "!!!not-base64!!!"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_next_token"


def test_get_product(client):
    _create(client)
    response = client.get("/products/SKU-1")
    assert response.status_code == 200
    assert response.json()["name"] == "Tea"


def test_get_product_not_found(client):
    response = client.get("/products/missing")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_patch_product(client):
    _create(client)
    response = client.patch("/products/SKU-1", json={"name": "Green Tea", "price": 4.25})
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Green Tea"
    assert body["price"] == 4.25
    assert body["quantity"] == 10


def test_patch_product_requires_a_field(client):
    _create(client)
    response = client.patch("/products/SKU-1", json={})
    assert response.status_code == 400
    assert response.json()["error"] == "no_fields"


def test_patch_product_not_found(client):
    response = client.patch("/products/nope", json={"name": "x"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_adjust_stock_up_and_down(client):
    _create(client, quantity=5)

    up = client.post("/products/SKU-1/adjust-stock", json={"delta": 7, "reason": "restock"})
    assert up.status_code == 200
    body = up.json()
    assert body["quantity"] == 12
    assert body["applied_delta"] == 7
    assert body["reason"] == "restock"

    down = client.post("/products/SKU-1/adjust-stock", json={"delta": -4})
    assert down.status_code == 200
    assert down.json()["quantity"] == 8


def test_adjust_stock_insufficient(client):
    _create(client, quantity=2)
    response = client.post("/products/SKU-1/adjust-stock", json={"delta": -5})
    assert response.status_code == 409
    assert response.json()["error"] == "insufficient_stock"
    assert client.get("/products/SKU-1").json()["quantity"] == 2


def test_adjust_stock_zero_delta(client):
    _create(client)
    response = client.post("/products/SKU-1/adjust-stock", json={"delta": 0})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_delta"


def test_adjust_stock_unknown_sku(client):
    response = client.post("/products/ghost/adjust-stock", json={"delta": 1})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_full_flow_against_stub_dynamodb(dynamo_client):
    test_client, table = dynamo_client

    assert _create(test_client, sku="DYN-1", quantity=4).status_code == 201
    assert _create(test_client, sku="DYN-1").status_code == 409

    assert test_client.get("/products/DYN-1").json()["quantity"] == 4
    assert test_client.get("/products/DYN-404").status_code == 404

    listing = test_client.get("/products", params={"limit": 10}).json()
    assert listing["count"] == 1
    assert table.scan_calls[-1]["Limit"] == 10

    patched = test_client.patch("/products/DYN-1", json={"price": 9.99}).json()
    assert patched["price"] == 9.99

    adjusted = test_client.post("/products/DYN-1/adjust-stock", json={"delta": -3}).json()
    assert adjusted["quantity"] == 1

    too_much = test_client.post("/products/DYN-1/adjust-stock", json={"delta": -9})
    assert too_much.status_code == 409
    assert too_much.json()["error"] == "insufficient_stock"

    missing = test_client.post("/products/DYN-none/adjust-stock", json={"delta": 1})
    assert missing.status_code == 404

    assert test_client.get("/health").json()["status"] == "ok"


def test_dynamo_repo_pagination_token_roundtrip():
    table = FakeTable()
    table.items["A"] = {"sku": "A", "name": "a", "price": 1, "quantity": 1}
    table.last_evaluated_key = {"sku": "A"}
    repo = storage.DynamoDBProductRepository(table_name="t", table=table)

    items, token = repo.list_products(limit=1)
    assert [item["sku"] for item in items] == ["A"]
    assert token

    table.last_evaluated_key = None
    items, token = repo.list_products(limit=1, next_token=token)
    assert token is None
    assert table.scan_calls[-1]["ExclusiveStartKey"] == {"sku": "A"}


def test_dynamo_repo_update_missing_raises():
    repo = storage.DynamoDBProductRepository(table_name="t", table=FakeTable())
    with pytest.raises(storage.ProductNotFound):
        repo.update_attributes("nope", name="x")


def test_dynamo_repo_health_failure_returns_false():
    repo = storage.DynamoDBProductRepository(table_name="t", table=FakeTable(fail_load=True))
    assert repo.healthy() is False


def test_dynamodb_resource_uses_endpoint_and_region(monkeypatch):
    captured = {}

    def fake_resource(service_name, **kwargs):
        captured["service"] = service_name
        captured.update(kwargs)
        return "fake-resource"

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    assert storage.dynamodb_resource() == "fake-resource"
    assert captured["service"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "us-east-1"


def test_table_name_from_environment(monkeypatch):
    monkeypatch.delenv("PRODUCTS_TABLE", raising=False)
    assert storage.products_table_name() == "shop-inventory-products"
    monkeypatch.setenv("PRODUCTS_TABLE", "custom-table")
    assert storage.products_table_name() == "custom-table"


def test_get_repository_is_cached(monkeypatch):
    created = []

    class DummyRepo:
        def __init__(self):
            created.append(self)

    monkeypatch.setattr(app_module, "DynamoDBProductRepository", DummyRepo)
    app_module._repository = None
    first = app_module.get_repository()
    second = app_module.get_repository()
    app_module._repository = None

    assert first is second
    assert len(created) == 1
