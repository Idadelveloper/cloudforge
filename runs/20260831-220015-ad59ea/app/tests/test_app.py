"""Offline tests for the shop inventory API (no AWS or network access)."""

import os
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage  # noqa: E402


def client_error(code: str) -> ClientError:
    """Build a botocore ClientError with the given error code."""
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


class StubDynamoClient:
    """Stub for table.meta.client."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.described = []

    def describe_table(self, **kwargs):
        if self.fail:
            raise client_error("ResourceNotFoundException")
        self.described.append(kwargs.get("TableName"))
        return {"Table": {"TableName": kwargs.get("TableName")}}


class StubTable:
    """Minimal stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, client=None) -> None:
        self.meta = SimpleNamespace(client=client or StubDynamoClient())
        self.calls = []
        self.put_error = None
        self.get_response = {}
        self.scan_response = {"Items": []}
        self.update_error = None
        self.update_response = {"Attributes": {}}

    def put_item(self, **kwargs):
        self.calls.append(("put_item", kwargs))
        if self.put_error is not None:
            raise self.put_error
        return {}

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs))
        return self.get_response

    def scan(self, **kwargs):
        self.calls.append(("scan", kwargs))
        return self.scan_response

    def update_item(self, **kwargs):
        self.calls.append(("update_item", kwargs))
        if self.update_error is not None:
            raise self.update_error
        return self.update_response


class BrokenRepository(storage.ProductRepository):
    """Repository whose every operation fails with a StorageError."""

    def ping(self):
        return False

    def create_product(self, sku, name, price, quantity):
        raise storage.StorageError("boom")

    def get_product(self, sku):
        raise storage.StorageError("boom")

    def list_products(self, limit=50, cursor=None):
        raise storage.StorageError("boom")

    def adjust_stock(self, sku, delta):
        raise storage.StorageError("boom")


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
def broken_client():
    app_module.app.dependency_overrides[app_module.get_repository] = BrokenRepository
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def make_product(client, sku="SKU-1", name="Widget", price=9.99, quantity=5):
    return client.post(
        "/products",
        json={"sku": sku, "name": name, "price": price, "quantity": quantity},
    )


# --------------------------------------------------------------------------- #
# Endpoint tests
# --------------------------------------------------------------------------- #
def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dynamodb"] == "ok"
    assert body["table"]


def test_health_reports_unavailable_backend(broken_client):
    body = broken_client.get("/health").json()
    assert body["status"] == "ok"
    assert body["dynamodb"] == "unavailable"


def test_create_product(client):
    response = make_product(client)
    assert response.status_code == 201
    body = response.json()
    assert body["sku"] == "SKU-1"
    assert body["name"] == "Widget"
    assert float(body["price"]) == pytest.approx(9.99)
    assert body["quantity"] == 5
    assert body["created_at"] and body["updated_at"]


def test_create_product_defaults_quantity_to_zero(client):
    response = client.post("/products", json={"sku": "SKU-Z", "name": "Zero", "price": 1})
    assert response.status_code == 201
    assert response.json()["quantity"] == 0


def test_create_duplicate_sku_conflicts(client):
    assert make_product(client).status_code == 201
    response = make_product(client)
    assert response.status_code == 409
    assert response.json()["code"] == "product_exists"


def test_create_product_validation_error(client):
    response = client.post("/products", json={"sku": "", "name": "x", "price": -1})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"]


def test_create_product_storage_failure(broken_client):
    response = broken_client.post(
        "/products", json={"sku": "SKU-1", "name": "Widget", "price": 1.0, "quantity": 1}
    )
    assert response.status_code == 503
    assert response.json()["code"] == "storage_error"


def test_list_products(client):
    make_product(client, sku="SKU-1")
    make_product(client, sku="SKU-2", name="Gadget")
    response = client.get("/products")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["next_cursor"] is None
    assert {item["sku"] for item in body["items"]} == {"SKU-1", "SKU-2"}


def test_list_products_pagination(client):
    for index in range(3):
        make_product(client, sku="SKU-%d" % index)
    first = client.get("/products", params={"limit": 2}).json()
    assert first["count"] == 2
    assert first["next_cursor"]
    second = client.get("/products", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert second["count"] == 1
    assert second["next_cursor"] is None
    seen = [item["sku"] for item in first["items"]] + [item["sku"] for item in second["items"]]
    assert sorted(seen) == ["SKU-0", "SKU-1", "SKU-2"]


def test_list_products_invalid_cursor(client):
    response = client.get("/products", params={"cursor": "!!!not-base64!!!"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


def test_list_products_limit_out_of_range(client):
    assert client.get("/products", params={"limit": 0}).status_code == 422
    assert client.get("/products", params={"limit": 1000}).status_code == 422


def test_list_products_storage_failure(broken_client):
    response = broken_client.get("/products")
    assert response.status_code == 503
    assert response.json()["code"] == "storage_error"


def test_get_product(client):
    make_product(client)
    response = client.get("/products/SKU-1")
    assert response.status_code == 200
    assert response.json()["sku"] == "SKU-1"


def test_get_product_not_found(client):
    response = client.get("/products/NOPE")
    assert response.status_code == 404
    assert response.json()["code"] == "product_not_found"


def test_get_product_storage_failure(broken_client):
    response = broken_client.get("/products/SKU-1")
    assert response.status_code == 503


def test_adjust_stock_up_and_down(client):
    make_product(client, quantity=5)
    up = client.post("/products/SKU-1/adjust-stock", json={"delta": 7, "reason": "delivery"})
    assert up.status_code == 200
    assert up.json()["quantity"] == 12
    down = client.post("/products/SKU-1/adjust-stock", json={"delta": -4})
    assert down.status_code == 200
    assert down.json()["quantity"] == 8


def test_adjust_stock_below_zero_conflicts(client):
    make_product(client, quantity=2)
    response = client.post("/products/SKU-1/adjust-stock", json={"delta": -3})
    assert response.status_code == 409
    assert response.json()["code"] == "insufficient_stock"
    assert client.get("/products/SKU-1").json()["quantity"] == 2


def test_adjust_stock_unknown_sku(client):
    response = client.post("/products/MISSING/adjust-stock", json={"delta": 1})
    assert response.status_code == 404
    assert response.json()["code"] == "product_not_found"


def test_adjust_stock_zero_delta_rejected(client):
    make_product(client)
    response = client.post("/products/SKU-1/adjust-stock", json={"delta": 0})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_adjust_stock_storage_failure(broken_client):
    response = broken_client.post("/products/SKU-1/adjust-stock", json={"delta": 1})
    assert response.status_code == 503


def test_unknown_route_uses_error_shape(client):
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert "detail" in body


# --------------------------------------------------------------------------- #
# Storage layer tests (stubbed boto3 table, no network)
# --------------------------------------------------------------------------- #
def test_dynamodb_resource_uses_endpoint_and_region(monkeypatch):
    captured = {}

    def fake_resource(service_name, **kwargs):
        captured["service"] = service_name
        captured.update(kwargs)
        return "stub-resource"

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert storage.dynamodb_resource() == "stub-resource"
    assert captured["service"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "eu-west-1"


def test_dynamodb_resource_defaults(monkeypatch):
    captured = {}

    def fake_resource(service_name, **kwargs):
        captured.update(kwargs)
        return "stub-resource"

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    storage.dynamodb_resource()
    assert captured["region_name"] == "us-east-1"
    assert captured["endpoint_url"] is None


def test_table_name_from_env(monkeypatch):
    monkeypatch.delenv("DYNAMODB_TABLE_NAME", raising=False)
    assert storage.table_name() == "shop-inventory-products"
    monkeypatch.setenv("DYNAMODB_TABLE_NAME", "custom-table")
    assert storage.table_name() == "custom-table"


def test_dynamo_create_product_puts_conditional_item():
    table = StubTable()
    repository = storage.DynamoProductRepository(name="t", table=table)
    product = repository.create_product("SKU-1", "Widget", Decimal("3.50"), 4)
    assert product["sku"] == "SKU-1"
    assert product["quantity"] == 4
    assert product["price"] == Decimal("3.50")
    kind, kwargs = table.calls[0]
    assert kind == "put_item"
    assert kwargs["ConditionExpression"] == "attribute_not_exists(#sku)"
    assert isinstance(kwargs["Item"]["price"], Decimal)


def test_dynamo_create_product_duplicate():
    table = StubTable()
    table.put_error = client_error("ConditionalCheckFailedException")
    repository = storage.DynamoProductRepository(name="t", table=table)
    with pytest.raises(storage.ProductAlreadyExists):
        repository.create_product("SKU-1", "Widget", Decimal("1"), 0)


def test_dynamo_create_product_other_error():
    table = StubTable()
    table.put_error = client_error("ProvisionedThroughputExceededException")
    repository = storage.DynamoProductRepository(name="t", table=table)
    with pytest.raises(storage.StorageError):
        repository.create_product("SKU-1", "Widget", Decimal("1"), 0)


def test_dynamo_get_product():
    table = StubTable()
    repository = storage.DynamoProductRepository(name="t", table=table)
    assert repository.get_product("missing") is None
    table.get_response = {
        "Item": {
            "sku": "SKU-1",
            "name": "Widget",
            "price": Decimal("2.25"),
            "quantity": Decimal("7"),
            "created_at": "now",
            "updated_at": "now",
        }
    }
    product = repository.get_product("SKU-1")
    assert product["quantity"] == 7
    assert product["price"] == Decimal("2.25")


def test_dynamo_list_products_cursor_roundtrip():
    table = StubTable()
    table.scan_response = {
        "Items": [{"sku": "SKU-1", "name": "W", "price": Decimal("1"), "quantity": Decimal("1")}],
        "LastEvaluatedKey": {"sku": "SKU-1"},
    }
    repository = storage.DynamoProductRepository(name="t", table=table)
    items, cursor = repository.list_products(limit=1)
    assert len(items) == 1
    assert cursor
    repository.list_products(limit=1, cursor=cursor)
    _, kwargs = table.calls[-1]
    assert kwargs["ExclusiveStartKey"] == {"sku": "SKU-1"}


def test_dynamo_list_products_invalid_cursor():
    repository = storage.DynamoProductRepository(name="t", table=StubTable())
    with pytest.raises(storage.InvalidCursor):
        repository.list_products(cursor="###")


def test_dynamo_adjust_stock_uses_conditional_update():
    table = StubTable()
    table.update_response = {
        "Attributes": {
            "sku": "SKU-1",
            "name": "Widget",
            "price": Decimal("1"),
            "quantity": Decimal("9"),
            "created_at": "now",
            "updated_at": "later",
        }
    }
    repository = storage.DynamoProductRepository(name="t", table=table)
    product = repository.adjust_stock("SKU-1", -3)
    assert product["quantity"] == 9
    _, kwargs = table.calls[-1]
    assert kwargs["ExpressionAttributeValues"][":needed"] == Decimal(3)
    assert "#qty >= :needed" in kwargs["ConditionExpression"]
    assert kwargs["ReturnValues"] == "ALL_NEW"


def test_dynamo_adjust_stock_missing_product():
    table = StubTable()
    table.update_error = client_error("ConditionalCheckFailedException")
    table.get_response = {}
    repository = storage.DynamoProductRepository(name="t", table=table)
    with pytest.raises(storage.ProductNotFound):
        repository.adjust_stock("SKU-1", -1)


def test_dynamo_adjust_stock_insufficient():
    table = StubTable()
    table.update_error = client_error("ConditionalCheckFailedException")
    table.get_response = {"Item": {"sku": "SKU-1", "name": "W", "price": Decimal("1"), "quantity": Decimal("1")}}
    repository = storage.DynamoProductRepository(name="t", table=table)
    with pytest.raises(storage.InsufficientStock):
        repository.adjust_stock("SKU-1", -5)


def test_dynamo_adjust_stock_other_error():
    table = StubTable()
    table.update_error = client_error("InternalServerError")
    repository = storage.DynamoProductRepository(name="t", table=table)
    with pytest.raises(storage.StorageError):
        repository.adjust_stock("SKU-1", 1)


def test_dynamo_ping():
    ok_table = StubTable()
    assert storage.DynamoProductRepository(name="t", table=ok_table).ping() is True
    assert ok_table.meta.client.described == ["t"]
    bad_table = StubTable(client=StubDynamoClient(fail=True))
    assert storage.DynamoProductRepository(name="t", table=bad_table).ping() is False


def test_get_repository_is_lazy_singleton(monkeypatch):
    monkeypatch.setattr(app_module, "_repository", None, raising=False)
    first = app_module.get_repository()
    second = app_module.get_repository()
    assert first is second
    assert isinstance(first, storage.DynamoProductRepository)
    monkeypatch.setattr(app_module, "_repository", None, raising=False)
