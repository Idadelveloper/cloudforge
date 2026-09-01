"""Offline tests for the expense tracker API and its storage layer."""
import pytest
from fastapi.testclient import TestClient

import app as app_module
import storage
from storage import (
    ExpenseNotFoundError,
    ExpenseRepository,
    InvalidCursorError,
    StorageError,
)


class FakeRepository(ExpenseRepository):
    """In-memory stand-in for the DynamoDB repository."""

    def __init__(self):
        self.items = {}
        self.healthy = True

    def health(self):
        if not self.healthy:
            raise StorageError("dynamodb unreachable")
        return {"name": "dynamodb", "status": "ok", "table": "expenses", "table_status": "ACTIVE"}

    def put(self, item):
        self.items[(item["user_id"], item["expense_id"])] = dict(item)
        return item

    def get(self, user_id, expense_id):
        found = self.items.get((user_id, expense_id))
        return dict(found) if found else None

    def update(self, user_id, expense_id, changes):
        key = (user_id, expense_id)
        if key not in self.items:
            raise ExpenseNotFoundError(expense_id)
        self.items[key].update(changes)
        return dict(self.items[key])

    def delete(self, user_id, expense_id):
        key = (user_id, expense_id)
        if key not in self.items:
            raise ExpenseNotFoundError(expense_id)
        return self.items.pop(key)

    def list_expenses(self, user_id, category=None, month=None, limit=50, cursor=None):
        if cursor == "not-a-cursor":
            raise InvalidCursorError("bad cursor")
        rows = [dict(value) for (owner, _), value in self.items.items() if owner == user_id]
        if category:
            rows = [row for row in rows if row.get("category") == category]
        if month:
            rows = [row for row in rows if str(row.get("date", "")).startswith(month)]
        rows.sort(key=lambda row: str(row.get("date", "")))
        return rows[:limit], None

    def iter_month(self, user_id, month):
        rows, _ = self.list_expenses(user_id, month=month, limit=1000)
        return rows


class FakeTable:
    """Records boto3 table calls and returns canned responses."""

    def __init__(self, responses=None, error=None):
        self.calls = []
        self.responses = responses or {}
        self.error = error

    def _respond(self, name, kwargs):
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return self.responses.get(name, {})

    def put_item(self, **kwargs):
        return self._respond("put_item", kwargs)

    def get_item(self, **kwargs):
        return self._respond("get_item", kwargs)

    def update_item(self, **kwargs):
        return self._respond("update_item", kwargs)

    def delete_item(self, **kwargs):
        return self._respond("delete_item", kwargs)

    def query(self, **kwargs):
        return self._respond("query", kwargs)


class FakeClientError(Exception):
    """Mimics botocore's ClientError shape."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@pytest.fixture()
def repo():
    fake = FakeRepository()
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: fake
    yield fake
    app_module.app.dependency_overrides.clear()


@pytest.fixture()
def client(repo):
    with TestClient(app_module.app) as test_client:
        yield test_client


def create_expense(client, **overrides):
    payload = {"amount": 12.5, "category": "groceries", "date": "2024-03-04"}
    payload.update(overrides)
    return client.post("/expenses", json=payload)


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"][0]["status"] == "ok"


def test_health_reports_unavailable_backend(client, repo):
    repo.healthy = False
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["dependencies"][0]["status"] == "unavailable"


def test_create_expense(client, repo):
    response = create_expense(client, description="weekly shop")
    assert response.status_code == 201
    body = response.json()
    assert body["amount"] == 12.5
    assert body["category"] == "groceries"
    assert body["month"] == "2024-03"
    assert body["currency"] == "USD"
    assert body["user_id"] == "default"
    assert body["expense_id"]
    assert len(repo.items) == 1


def test_create_expense_rejects_bad_date(client):
    response = create_expense(client, date="04/03/2024")
    assert response.status_code == 400


def test_create_expense_rejects_non_positive_amount(client):
    response = create_expense(client, amount=-3)
    assert response.status_code in (400, 422)


def test_get_expense_roundtrip(client):
    created = create_expense(client).json()
    response = client.get("/expenses/%s" % created["expense_id"])
    assert response.status_code == 200
    assert response.json()["expense_id"] == created["expense_id"]


def test_get_expense_missing(client):
    response = client.get("/expenses/does-not-exist")
    assert response.status_code == 404


def test_list_expenses_with_filters(client):
    create_expense(client)
    create_expense(client, category="transport", date="2024-04-05", amount=30)

    everything = client.get("/expenses")
    assert everything.status_code == 200
    assert everything.json()["count"] == 2

    by_category = client.get("/expenses", params={"category": "groceries"})
    assert by_category.status_code == 200
    body = by_category.json()
    assert body["count"] == 1
    assert body["filters"]["category"] == "groceries"

    by_month = client.get("/expenses", params={"month": "2024-04"})
    assert by_month.json()["count"] == 1
    assert by_month.json()["items"][0]["category"] == "transport"


def test_list_expenses_rejects_bad_month(client):
    response = client.get("/expenses", params={"month": "2024-13"})
    assert response.status_code == 400


def test_list_expenses_rejects_bad_cursor(client):
    response = client.get("/expenses", params={"cursor": "not-a-cursor"})
    assert response.status_code == 400


def test_summary_totals_per_category(client):
    create_expense(client, amount=10.0)
    create_expense(client, amount=5.25, date="2024-03-11")
    create_expense(client, amount=20.0, category="transport", date="2024-03-15")
    create_expense(client, amount=99.0, date="2024-04-01")

    response = client.get("/expenses/summary", params={"month": "2024-03"})
    assert response.status_code == 200
    body = response.json()
    assert body["month"] == "2024-03"
    assert body["expense_count"] == 3
    assert body["grand_total"] == 35.25
    totals = {row["category"]: row for row in body["totals_by_category"]}
    assert totals["groceries"]["total"] == 15.25
    assert totals["groceries"]["expense_count"] == 2
    assert totals["transport"]["total"] == 20.0


def test_summary_requires_month(client):
    response = client.get("/expenses/summary")
    assert response.status_code == 422


def test_update_expense(client):
    created = create_expense(client).json()
    response = client.put(
        "/expenses/%s" % created["expense_id"],
        json={"amount": 40, "category": "dining", "date": "2024-05-02"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["amount"] == 40.0
    assert body["category"] == "dining"
    assert body["month"] == "2024-05"


def test_update_expense_missing(client):
    response = client.put("/expenses/nope", json={"amount": 5})
    assert response.status_code == 404


def test_update_expense_requires_fields(client):
    created = create_expense(client).json()
    response = client.put("/expenses/%s" % created["expense_id"], json={})
    assert response.status_code == 400


def test_delete_expense(client):
    created = create_expense(client).json()
    response = client.delete("/expenses/%s" % created["expense_id"])
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.get("/expenses/%s" % created["expense_id"]).status_code == 404


def test_delete_expense_missing(client):
    assert client.delete("/expenses/unknown").status_code == 404


def test_storage_error_maps_to_503(client, repo):
    def boom(*_args, **_kwargs):
        raise StorageError("table gone")

    repo.list_expenses = boom
    response = client.get("/expenses")
    assert response.status_code == 503


def test_dynamodb_resource_uses_endpoint_override(monkeypatch):
    captured = {}

    def fake_resource(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    storage.dynamodb_resource()
    assert captured["service"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "us-east-1"


def test_dynamodb_client_defaults_to_no_endpoint(monkeypatch):
    captured = {}

    def fake_client(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(storage.boto3, "client", fake_client)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    storage.dynamodb_client()
    assert captured["endpoint_url"] is None
    assert captured["region_name"] == "us-east-1"


def _repository_with_table(table):
    repository = storage.DynamoDBExpenseRepository(table_name="expenses-test")
    repository._table = table
    return repository


def test_repository_list_uses_category_index():
    table = FakeTable({"query": {"Items": [{"expense_id": "a"}]}})
    repository = _repository_with_table(table)
    items, cursor = repository.list_expenses("default", category="groceries", month="2024-03")
    assert items == [{"expense_id": "a"}]
    assert cursor is None
    name, kwargs = table.calls[0]
    assert name == "query"
    assert kwargs["IndexName"] == "category-date-index"
    assert "FilterExpression" in kwargs


def test_repository_list_uses_month_index_and_cursor():
    table = FakeTable({"query": {"Items": [], "LastEvaluatedKey": {"user_id": "u", "expense_id": "e"}}})
    repository = _repository_with_table(table)
    _items, cursor = repository.list_expenses("default", month="2024-03")
    assert cursor is not None
    assert storage.decode_cursor(cursor) == {"user_id": "u", "expense_id": "e"}
    _name, kwargs = table.calls[0]
    assert kwargs["IndexName"] == "month-date-index"


def test_repository_iter_month_paginates():
    class PagingTable(FakeTable):
        def __init__(self):
            super().__init__()
            self.page = 0

        def query(self, **kwargs):
            self.calls.append(("query", kwargs))
            self.page += 1
            if self.page == 1:
                return {"Items": [{"amount": 1}], "LastEvaluatedKey": {"user_id": "u"}}
            return {"Items": [{"amount": 2}]}

    repository = _repository_with_table(PagingTable())
    items = repository.iter_month("default", "2024-03")
    assert len(items) == 2


def test_repository_update_missing_item_raises_not_found():
    table = FakeTable(error=FakeClientError("ConditionalCheckFailedException"))
    repository = _repository_with_table(table)
    with pytest.raises(ExpenseNotFoundError):
        repository.update("default", "missing", {"amount": 1})


def test_repository_update_other_error_raises_storage_error():
    table = FakeTable(error=FakeClientError("ProvisionedThroughputExceededException"))
    repository = _repository_with_table(table)
    with pytest.raises(StorageError):
        repository.update("default", "any", {"amount": 1})


def test_repository_delete_missing_item_raises_not_found():
    table = FakeTable({"delete_item": {}})
    repository = _repository_with_table(table)
    with pytest.raises(ExpenseNotFoundError):
        repository.delete("default", "missing")


def test_repository_put_and_get():
    table = FakeTable({"get_item": {"Item": {"expense_id": "x", "amount": 3}}})
    repository = _repository_with_table(table)
    repository.put({"user_id": "default", "expense_id": "x", "amount": 3.5, "description": None})
    stored = table.calls[0][1]["Item"]
    assert "description" not in stored
    assert str(stored["amount"]) == "3.5"
    assert repository.get("default", "x")["expense_id"] == "x"


def test_decode_cursor_rejects_garbage():
    with pytest.raises(InvalidCursorError):
        storage.decode_cursor("%%%not-base64%%%")
