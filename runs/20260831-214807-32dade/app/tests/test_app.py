"""Offline tests for the shop inventory API.

Every AWS interaction is served by an in-memory fake DynamoDB table, so the suite
never touches the network or LocalStack.
"""

import os
import sys
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from storage import (  # noqa: E402
    DynamoDBProductRepository,
    InsufficientStockError,
    InvalidPaginationTokenError,
    ProductExistsError,
    ProductNotFoundError,
    decode_token,
    encode_token,
)


class FakeClientError(Exception):
    """Mimics botocore ClientError shape (has a .response dict)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class FakeTable:
    """Minimal in-memory stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, broken: bool = False) -> None:
        self.items = {}
        self.broken = broken

    def put_item(self, **kwargs):
        if self.broken:
            raise FakeClientError("InternalServerError")
        item = dict(kwargs["Item"])
        if kwargs.get("ConditionExpression") and item["sku"] in self.items:
            raise FakeClientError("ConditionalCheckFailedException")
        self.items[item["sku"]] = item
        return {}

    def get_item(self, **kwargs):
        if self.broken:
            raise FakeClientError("ResourceNotFoundException")
        item = self.items.get(kwargs["Key"]["sku"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs):
        if self.broken:
            raise FakeClientError("InternalServerError")
        sku = kwargs["Key"]["sku"]
        values = kwargs["ExpressionAttributeValues"]
        item = self.items.get(sku)
        if item is None:
            raise FakeClientError("ConditionalCheckFailedException")
        minimum = values.get(":min_quantity")
        if minimum is not None and Decimal(item["quantity"]) < Decimal(minimum):
            raise FakeClientError("ConditionalCheckFailedException")
        item["quantity"] = Decimal(item["quantity"]) + Decimal(values[":delta"])
        item["updated_at"] = values[":now"]
        return {"Attributes": dict(item)}

    def scan(self, **kwargs):
        if self.broken:
            raise FakeClientError("InternalServerError")
        limit = int(kwargs.get("Limit", 50))
        keys = sorted(self.items)
        start = kwargs.get("ExclusiveStartKey")
        if start:
            keys = [key for key in keys if key > start["sku"]]
        page = keys[:limit]
        result = {"Items": [dict(self.items[key]) for key in page]}
        if page and len(keys) > limit:
            result["LastEvaluatedKey"] = {"sku": page[-1]}
        return result


class FakeResource:
    """Stand-in for boto3.resource('dynamodb')."""

    def __init__(self, table: FakeTable) -> None:
        self._table = table

    def Table(self, name):  # noqa: N802 - mirrors the boto3 API
        self.table_name = name
        return self._table


@pytest.fixture
def table():
    return FakeTable()


@pytest.fixture
def repo(table):
    return DynamoDBProductRepository(table_name="products", resource=FakeResource(table))


@pytest.fixture
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _create(client, sku="SKU-1", name="Widget", price=9.99, quantity=5):
    return client.post(
        "/products",
        json={"sku": sku, "name": name, "price": price, "quantity": quantity},
    )


# --------------------------------------------------------------------- health
def test_health_reports_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dynamodb"] == "reachable"
    assert body["table"]


def test_health_reports_degraded_when_table_unreachable():
    broken_repo = DynamoDBProductRepository(
        table_name="products", resource=FakeResource(FakeTable(broken=True))
    )
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: broken_repo
    try:
        with TestClient(app_module.app) as test_client:
            body = test_client.get("/health").json()
        assert body["status"] == "degraded"
        assert body["dynamodb"] == "unreachable"
    finally:
        app_module.app.dependency_overrides.clear()


# -------------------------------------------------------------------- create
def test_create_product_returns_201(client):
    response = _create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["sku"] == "SKU-1"
    assert body["name"] == "Widget"
    assert Decimal(str(body["price"])) == Decimal("9.99")
    assert body["quantity"] == 5
    assert body["created_at"] and body["updated_at"]


def test_create_defaults_quantity_to_zero(client):
    response = client.post("/products", json={"sku": "SKU-Z", "name": "Zero", "price": 1})
    assert response.status_code == 201
    assert response.json()["quantity"] == 0


def test_create_duplicate_sku_returns_409(client):
    assert _create(client).status_code == 201
    response = _create(client)
    assert response.status_code == 409
    assert response.json()["code"] == "PRODUCT_EXISTS"


@pytest.mark.parametrize(
    "payload",
    [
        {"sku": "", "name": "Widget", "price": 1, "quantity": 1},
        {"sku": "SKU-2", "name": "", "price": 1, "quantity": 1},
        {"sku": "SKU-2", "name": "Widget", "price": -1, "quantity": 1},
        {"sku": "SKU-2", "name": "Widget", "price": 1, "quantity": -3},
        {"name": "Widget", "price": 1},
    ],
)
def test_create_invalid_payload_returns_422(client, payload):
    assert client.post("/products", json=payload).status_code == 422


# ----------------------------------------------------------------- read paths
def test_get_product_returns_item(client):
    _create(client, sku="SKU-7", name="Bolt", price=2.5, quantity=3)
    response = client.get("/products/SKU-7")
    assert response.status_code == 200
    body = response.json()
    assert body["sku"] == "SKU-7"
    assert body["quantity"] == 3
    assert Decimal(str(body["price"])) == Decimal("2.5")


def test_get_unknown_product_returns_404(client):
    response = client.get("/products/NOPE")
    assert response.status_code == 404
    assert response.json()["code"] == "PRODUCT_NOT_FOUND"


def test_list_products_returns_all(client):
    _create(client, sku="A-1")
    _create(client, sku="A-2")
    response = client.get("/products")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["next_token"] is None
    assert {item["sku"] for item in body["items"]} == {"A-1", "A-2"}


def test_list_products_paginates(client):
    for index in range(3):
        _create(client, sku="P-{0}".format(index))
    first = client.get("/products", params={"limit": 2}).json()
    assert first["count"] == 2
    assert first["next_token"]
    second = client.get("/products", params={"limit": 2, "next_token": first["next_token"]}).json()
    assert second["count"] == 1
    assert second["next_token"] is None
    seen = [item["sku"] for item in first["items"]] + [item["sku"] for item in second["items"]]
    assert sorted(seen) == ["P-0", "P-1", "P-2"]


def test_list_products_rejects_bad_token(client):
    response = client.get("/products", params={"next_token": "not-a-token"})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_NEXT_TOKEN"


def test_list_products_rejects_out_of_range_limit(client):
    assert client.get("/products", params={"limit": 0}).status_code == 422
    assert client.get("/products", params={"limit": 1000}).status_code == 422


# ------------------------------------------------------------ stock adjustment
def test_adjust_stock_increases_quantity(client):
    _create(client, sku="S-1", quantity=5)
    response = client.patch("/products/S-1/stock", json={"delta": 4, "reason": "delivery"})
    assert response.status_code == 200
    assert response.json()["quantity"] == 9


def test_adjust_stock_decreases_quantity(client):
    _create(client, sku="S-2", quantity=5)
    response = client.patch("/products/S-2/stock", json={"delta": -5})
    assert response.status_code == 200
    assert response.json()["quantity"] == 0


def test_adjust_stock_below_zero_returns_409(client):
    _create(client, sku="S-3", quantity=2)
    response = client.patch("/products/S-3/stock", json={"delta": -3})
    assert response.status_code == 409
    assert response.json()["code"] == "INSUFFICIENT_STOCK"
    assert client.get("/products/S-3").json()["quantity"] == 2


def test_adjust_stock_unknown_sku_returns_404(client):
    response = client.patch("/products/GHOST/stock", json={"delta": 1})
    assert response.status_code == 404
    assert response.json()["code"] == "PRODUCT_NOT_FOUND"


def test_adjust_stock_zero_delta_returns_400(client):
    _create(client, sku="S-4", quantity=1)
    response = client.patch("/products/S-4/stock", json={"delta": 0})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_DELTA"


def test_adjust_stock_invalid_body_returns_422(client):
    _create(client, sku="S-5", quantity=1)
    assert client.patch("/products/S-5/stock", json={"delta": "lots"}).status_code == 422
    assert client.patch("/products/S-5/stock", json={}).status_code == 422


# ---------------------------------------------------------------- storage unit
def test_repository_create_duplicate_raises(repo):
    payload = {"sku": "R-1", "name": "Nut", "price": Decimal("1.25"), "quantity": 2}
    repo.create_product(payload)
    with pytest.raises(ProductExistsError):
        repo.create_product(payload)


def test_repository_adjust_missing_raises(repo):
    with pytest.raises(ProductNotFoundError):
        repo.adjust_stock("missing", 1)


def test_repository_adjust_insufficient_raises(repo):
    repo.create_product({"sku": "R-2", "name": "Nut", "price": Decimal("1"), "quantity": 1})
    with pytest.raises(InsufficientStockError):
        repo.adjust_stock("R-2", -2)


def test_repository_get_missing_returns_none(repo):
    assert repo.get_product("nothing") is None


def test_repository_propagates_unexpected_errors():
    broken = DynamoDBProductRepository(
        table_name="products", resource=FakeResource(FakeTable(broken=True))
    )
    with pytest.raises(FakeClientError):
        broken.create_product({"sku": "X", "name": "X", "price": Decimal("1"), "quantity": 0})
    with pytest.raises(FakeClientError):
        broken.adjust_stock("X", 1)
    assert broken.healthy() is False


def test_token_round_trip():
    token = encode_token({"sku": "ABC"})
    assert decode_token(token) == {"sku": "ABC"}
    with pytest.raises(InvalidPaginationTokenError):
        decode_token("@@@not-base64@@@")


def test_get_repository_builds_dynamodb_repository(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("PRODUCTS_TABLE", "products")
    monkeypatch.setattr(app_module, "_repository", None, raising=False)
    try:
        built = app_module.get_repository()
        assert isinstance(built, DynamoDBProductRepository)
        assert built.table_name == "products"
        assert app_module.get_repository() is built
    finally:
        monkeypatch.setattr(app_module, "_repository", None, raising=False)
