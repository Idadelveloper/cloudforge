"""Offline tests for the expense tracker API.

Every AWS interaction is replaced with an in-memory fake, so the suite runs
without LocalStack, credentials or any network access.
"""

import copy

import pytest
from fastapi.testclient import TestClient

import app as app_module
import storage
from app import app, get_repository


class FakeRepository:
    """In-memory stand-in for DynamoDBExpenseRepository."""

    def __init__(self):
        self.items = []
        self.healthy = True
        self.deleted = []

    def health(self):
        return self.healthy

    def put_expense(self, item):
        self.items = [
            i for i in self.items
            if not (i["user_id"] == item["user_id"] and i["sk"] == item["sk"])
        ]
        self.items.append(copy.deepcopy(item))
        return copy.deepcopy(item)

    def get_expense(self, user_id, expense_id):
        for item in self.items:
            if item["user_id"] == user_id and item["expense_id"] == expense_id:
                return copy.deepcopy(item)
        return None

    def delete_expense(self, user_id, sort_key):
        self.deleted.append((user_id, sort_key))
        self.items = [
            i for i in self.items
            if not (i["user_id"] == user_id and i["sk"] == sort_key)
        ]

    def list_expenses(self, user_id, category=None, month=None, limit=50, cursor=None):
        rows = [i for i in self.items if i["user_id"] == user_id]
        if category:
            rows = [i for i in rows if i["category"] == category]
        if month:
            rows = [i for i in rows if i["sk"].startswith(month)]
        rows.sort(key=lambda i: i["sk"], reverse=True)
        page = [copy.deepcopy(i) for i in rows[:limit]]
        last_key = None
        if len(rows) > limit and page:
            last_key = {"user_id": user_id, "sk": page[-1]["sk"]}
        return page, last_key

    def iter_month_expenses(self, user_id, month):
        rows = [
            i for i in self.items
            if i["user_id"] == user_id and i["sk"].startswith(month)
        ]
        for item in sorted(rows, key=lambda i: i["sk"]):
            yield copy.deepcopy(item)


class FakeTable:
    """Minimal boto3 Table double used for repository unit tests."""

    def __init__(self, responses=None, status="ACTIVE"):
        self.table_status = status
        self.responses = responses or [{"Items": []}]
        self.queries = []
        self.puts = []
        self.deletes = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        index = min(len(self.queries) - 1, len(self.responses) - 1)
        return self.responses[index]

    def put_item(self, **kwargs):
        self.puts.append(kwargs)
        return {}

    def delete_item(self, **kwargs):
        self.deletes.append(kwargs)
        return {}


class BrokenTable:
    """Table double whose describe call always fails."""

    @property
    def table_status(self):
        raise RuntimeError("table missing")


@pytest.fixture()
def repo():
    return FakeRepository()


@pytest.fixture()
def client(repo):
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def create_expense(client, headers=None, **overrides):
    payload = {"amount": "10.50", "category": "Groceries", "date": "2024-03-05"}
    payload.update(overrides)
    return client.post("/expenses", json=payload, headers=headers or {})


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["table"] == storage.table_name()


def test_health_unavailable(client, repo):
    repo.healthy = False
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


def test_create_expense_normalises_fields(client, repo):
    response = create_expense(client, description="  weekly shop  ")
    assert response.status_code == 201
    body = response.json()
    assert body["amount"] == 10.5
    assert body["category"] == "groceries"
    assert body["month"] == "2024-03"
    assert body["currency"] == "USD"
    assert body["description"] == "weekly shop"
    assert body["user_id"] == app_module.DEFAULT_USER_ID
    assert body["expense_id"]
    stored = repo.items[0]
    assert stored["sk"] == "2024-03-05#%s" % body["expense_id"]
    assert stored["gsi1pk"] == "%s#groceries" % app_module.DEFAULT_USER_ID


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": "0"},
        {"amount": "-3.00"},
        {"category": "   "},
        {"date": "05-03-2024"},
        {"currency": "dollars"},
        {"description": "x" * 400},
    ],
)
def test_create_expense_validation_errors(client, overrides):
    response = create_expense(client, **overrides)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "bad_request"
    assert body["detail"]


def test_create_expense_rejects_non_numeric_amount(client):
    response = create_expense(client, amount="abc")
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_get_expense_roundtrip_and_404(client):
    created = create_expense(client).json()
    fetched = client.get("/expenses/%s" % created["expense_id"])
    assert fetched.status_code == 200
    assert fetched.json() == created

    missing = client.get("/expenses/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"


def test_list_expenses_with_filters(client):
    create_expense(client, amount="10.50", category="Groceries", date="2024-03-05")
    create_expense(client, amount="5.25", category="groceries", date="2024-03-20")
    create_expense(client, amount="20", category="Transport", date="2024-03-12")
    create_expense(client, amount="7.00", category="groceries", date="2024-04-02")

    all_items = client.get("/expenses")
    assert all_items.status_code == 200
    assert all_items.json()["count"] == 4
    dates = [i["date"] for i in all_items.json()["items"]]
    assert dates == sorted(dates, reverse=True)

    by_category = client.get("/expenses", params={"category": "GROCERIES"})
    assert by_category.json()["count"] == 3

    by_month = client.get("/expenses", params={"month": "2024-03"})
    assert by_month.json()["count"] == 3

    both = client.get("/expenses", params={"category": "groceries", "month": "2024-03"})
    assert both.json()["count"] == 2


def test_list_expenses_pagination_cursor(client):
    create_expense(client, date="2024-03-01")
    create_expense(client, date="2024-03-02")
    first = client.get("/expenses", params={"limit": 1})
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor
    second = client.get("/expenses", params={"limit": 1, "cursor": cursor})
    assert second.status_code == 200


def test_list_expenses_rejects_bad_inputs(client):
    assert client.get("/expenses", params={"month": "March"}).status_code == 400
    assert client.get("/expenses", params={"cursor": "!!!not-base64!!!"}).status_code == 400
    assert client.get("/expenses", params={"limit": 0}).status_code == 422


def test_update_expense_changes_sort_key(client, repo):
    created = create_expense(client).json()
    response = client.put(
        "/expenses/%s" % created["expense_id"],
        json={"amount": "33.333", "category": "Dining", "date": "2024-04-06"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["amount"] == 33.33
    assert body["category"] == "dining"
    assert body["month"] == "2024-04"
    assert body["created_at"] == created["created_at"]
    assert len(repo.items) == 1
    assert repo.deleted == [(app_module.DEFAULT_USER_ID, created["expense_id"].join(["2024-03-05#", ""]))]


def test_update_expense_not_found(client):
    response = client.put("/expenses/nope", json={"amount": "1.00"})
    assert response.status_code == 404


def test_delete_expense(client):
    created = create_expense(client).json()
    deleted = client.delete("/expenses/%s" % created["expense_id"])
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "expense_id": created["expense_id"]}
    assert client.get("/expenses/%s" % created["expense_id"]).status_code == 404
    assert client.delete("/expenses/%s" % created["expense_id"]).status_code == 404


def test_monthly_summary(client):
    create_expense(client, amount="10.50", category="Groceries", date="2024-03-05")
    create_expense(client, amount="5.25", category="groceries", date="2024-03-20")
    create_expense(client, amount="20.00", category="Transport", date="2024-03-12")
    create_expense(client, amount="99.00", category="groceries", date="2024-04-01")

    response = client.get("/summary", params={"month": "2024-03"})
    assert response.status_code == 200
    body = response.json()
    assert body["month"] == "2024-03"
    assert body["currency"] == "USD"
    assert body["expense_count"] == 3
    assert body["grand_total"] == 35.75
    assert body["totals_by_category"] == [
        {"category": "transport", "total": 20.0, "count": 1},
        {"category": "groceries", "total": 15.75, "count": 2},
    ]


def test_summary_requires_valid_month(client):
    assert client.get("/summary").status_code == 422
    assert client.get("/summary", params={"month": "2024"}).status_code == 400


def test_summary_empty_month(client):
    response = client.get("/summary", params={"month": "2030-01"})
    assert response.status_code == 200
    body = response.json()
    assert body["expense_count"] == 0
    assert body["grand_total"] == 0
    assert body["totals_by_category"] == []


def test_user_isolation_via_header(client):
    create_expense(client, headers={"X-User-Id": "alice"}, amount="11.00")
    create_expense(client, headers={"X-User-Id": "bob"}, amount="22.00")

    alice = client.get("/expenses", headers={"X-User-Id": "alice"}).json()
    bob = client.get("/expenses", headers={"X-User-Id": "bob"}).json()
    assert alice["count"] == 1
    assert bob["count"] == 1
    assert alice["items"][0]["amount"] == 11.0
    assert bob["items"][0]["user_id"] == "bob"

    alice_id = alice["items"][0]["expense_id"]
    assert client.get("/expenses/%s" % alice_id, headers={"X-User-Id": "bob"}).status_code == 404


def test_cursor_encode_decode_roundtrip():
    key = {"user_id": "u1", "sk": "2024-03-01#abc"}
    token = storage.encode_cursor(key)
    assert storage.decode_cursor(token) == key
    assert storage.encode_cursor(None) is None
    assert storage.decode_cursor(None) is None
    with pytest.raises(ValueError):
        storage.decode_cursor("!!!not-base64!!!")


def test_repository_list_uses_category_index():
    table = FakeTable([
        {"Items": [{"expense_id": "e1"}], "LastEvaluatedKey": {"user_id": "u1", "sk": "s1"}},
    ])
    repo = storage.DynamoDBExpenseRepository(table=table, index_name="cat-index")
    items, last_key = repo.list_expenses("u1", category="food", month="2024-03", limit=10)
    assert items == [{"expense_id": "e1"}]
    assert last_key == {"user_id": "u1", "sk": "s1"}
    query = table.queries[0]
    assert query["IndexName"] == "cat-index"
    assert query["Limit"] == 10
    assert query["ScanIndexForward"] is False


def test_repository_list_without_category_uses_base_table():
    table = FakeTable([{"Items": []}])
    repo = storage.DynamoDBExpenseRepository(table=table)
    items, last_key = repo.list_expenses("u1")
    assert items == []
    assert last_key is None
    assert "IndexName" not in table.queries[0]


def test_repository_get_expense_paginates():
    table = FakeTable([
        {"Items": [{"expense_id": "other", "sk": "a"}], "LastEvaluatedKey": {"user_id": "u1", "sk": "a"}},
        {"Items": [{"expense_id": "target", "sk": "b"}]},
    ])
    repo = storage.DynamoDBExpenseRepository(table=table)
    found = repo.get_expense("u1", "target")
    assert found == {"expense_id": "target", "sk": "b"}
    assert len(table.queries) == 2
    assert repo.get_expense("u1", "missing") is None


def test_repository_put_delete_and_month_iteration():
    table = FakeTable([
        {"Items": [{"expense_id": "e1"}], "LastEvaluatedKey": {"user_id": "u1", "sk": "x"}},
        {"Items": [{"expense_id": "e2"}]},
    ])
    repo = storage.DynamoDBExpenseRepository(table=table)
    repo.put_expense({"user_id": "u1", "sk": "2024-03-01#e1"})
    repo.delete_expense("u1", "2024-03-01#e1")
    assert table.puts == [{"Item": {"user_id": "u1", "sk": "2024-03-01#e1"}}]
    assert table.deletes == [{"Key": {"user_id": "u1", "sk": "2024-03-01#e1"}}]
    collected = list(repo.iter_month_expenses("u1", "2024-03"))
    assert collected == [{"expense_id": "e1"}, {"expense_id": "e2"}]


def test_repository_health_states():
    assert storage.DynamoDBExpenseRepository(table=FakeTable()).health() is True
    assert storage.DynamoDBExpenseRepository(table=BrokenTable()).health() is False


def test_dynamodb_resource_uses_environment(monkeypatch):
    captured = {}

    def fake_resource(service_name, **kwargs):
        captured["service_name"] = service_name
        captured.update(kwargs)
        return "fake-resource"

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert storage.dynamodb_resource() == "fake-resource"
    assert captured["service_name"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "eu-west-1"


def test_resource_names_from_environment(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("EXPENSES_TABLE", raising=False)
    monkeypatch.delenv("EXPENSES_CATEGORY_INDEX", raising=False)
    assert storage.region_name() == "us-east-1"
    assert storage.table_name() == "expenses"
    assert storage.category_index_name() == "expenses-gsi-category"

    monkeypatch.setenv("EXPENSES_TABLE", "other-table")
    assert storage.table_name() == "other-table"


def test_get_repository_is_cached(monkeypatch):
    monkeypatch.setattr(app_module, "_repository", None)
    first = app_module.get_repository()
    second = app_module.get_repository()
    assert first is second
    monkeypatch.setattr(app_module, "_repository", None)
