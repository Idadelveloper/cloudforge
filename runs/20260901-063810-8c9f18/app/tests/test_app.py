"""Offline tests for the product feedback service. No AWS access required."""
import os
import sys
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage  # noqa: E402


class FakeRepository:
    """In-memory stand-in for DynamoFeedbackRepository."""

    def __init__(self):
        self.items = []
        self.healthy = True
        self.put_calls = 0

    def put_feedback(self, item):
        self.put_calls += 1
        self.items = [entry for entry in self.items if entry["feedback_id"] != item["feedback_id"]]
        self.items.append(dict(item))
        return dict(item)

    def get_feedback(self, feedback_id):
        for entry in self.items:
            if entry["feedback_id"] == feedback_id:
                return dict(entry)
        return None

    def all_feedback(self, product_id=None):
        items = [dict(entry) for entry in self.items]
        if product_id:
            items = [entry for entry in items if entry.get("product_id") == product_id]
        return sorted(items, key=lambda entry: str(entry.get("created_at", "")), reverse=True)

    def list_feedback(self, product_id=None, min_rating=None, max_rating=None, limit=50):
        items = self.all_feedback(product_id)
        if min_rating is not None:
            items = [entry for entry in items if entry["rating"] >= min_rating]
        if max_rating is not None:
            items = [entry for entry in items if entry["rating"] <= max_rating]
        return items[:limit]

    def ping(self):
        return self.healthy


class FakeNotifier:
    """Records published alerts instead of calling SNS."""

    def __init__(self, result=True):
        self.result = result
        self.published = []
        self.healthy = True

    def publish_low_rating(self, alert):
        self.published.append(alert)
        return self.result

    def ping(self):
        return self.healthy


class FakeTable:
    """Minimal DynamoDB Table double."""

    table_status = "ACTIVE"

    def __init__(self, items=None, fail_query=False):
        self.items = list(items or [])
        self.fail_query = fail_query
        self.written = []
        self.query_calls = []
        self.scan_calls = []

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.written.append(item)
        self.items = [entry for entry in self.items if entry.get("feedback_id") != item.get("feedback_id")]
        self.items.append(item)
        return {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        for entry in self.items:
            if entry.get("feedback_id") == key.get("feedback_id"):
                return {"Item": entry}
        return {}

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if self.fail_query:
            raise RuntimeError("index missing")
        return {"Items": list(self.items)}

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        return {"Items": list(self.items)}


class FakeSnsClient:
    """Minimal SNS client double."""

    def __init__(self, fail=False):
        self.fail = fail
        self.published = []
        self.attribute_calls = []

    def create_topic(self, **kwargs):
        return {"TopicArn": "arn:aws:sns:us-east-1:000000000000:" + kwargs["Name"]}

    def publish(self, **kwargs):
        if self.fail:
            raise RuntimeError("sns unavailable")
        self.published.append(kwargs)
        return {"MessageId": "m-1"}

    def get_topic_attributes(self, **kwargs):
        self.attribute_calls.append(kwargs)
        return {"Attributes": {"TopicArn": kwargs["TopicArn"]}}


@pytest.fixture
def repo():
    return FakeRepository()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def client(repo, notifier):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    app_module.app.dependency_overrides[app_module.get_notifier] = lambda: notifier
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def test_create_feedback_high_rating_no_alert(client, repo, notifier):
    response = client.post("/feedback", json={"product_id": "widget", "rating": 5, "comment": "Excellent"})
    assert response.status_code == 201
    body = response.json()
    assert body["product_id"] == "widget"
    assert body["rating"] == 5
    assert body["alert_sent"] is False
    assert body["feedback_id"]
    assert body["created_at"].endswith("Z")
    assert notifier.published == []
    assert len(repo.items) == 1


def test_create_feedback_low_rating_triggers_alert(client, notifier):
    response = client.post(
        "/feedback",
        json={"product_id": "widget", "rating": 1, "comment": "Broken on arrival", "customer_email": "a@b.co"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["alert_sent"] is True
    assert body["customer_email"] == "a@b.co"
    assert len(notifier.published) == 1
    assert notifier.published[0]["rating"] == 1
    assert notifier.published[0]["product_id"] == "widget"


def test_create_feedback_alert_failure_still_stores(client, repo, notifier):
    notifier.result = False
    response = client.post("/feedback", json={"rating": 2, "comment": "Poor quality"})
    assert response.status_code == 201
    body = response.json()
    assert body["alert_sent"] is False
    assert body["product_id"] == "general"
    assert len(repo.items) == 1


def test_create_feedback_defaults_product_id(client):
    response = client.post("/feedback", json={"rating": 4, "comment": "Fine"})
    assert response.status_code == 201
    assert response.json()["product_id"] == "general"


@pytest.mark.parametrize(
    "payload",
    [
        {"rating": 6, "comment": "too high"},
        {"rating": 0, "comment": "too low"},
        {"rating": "five", "comment": "not a number"},
        {"comment": "missing rating"},
        {"rating": 3},
        {"rating": 3, "comment": ""},
    ],
)
def test_create_feedback_validation_errors(client, payload):
    assert client.post("/feedback", json=payload).status_code == 422


def test_list_feedback_with_filters(client):
    client.post("/feedback", json={"product_id": "a", "rating": 5, "comment": "great"})
    client.post("/feedback", json={"product_id": "b", "rating": 2, "comment": "meh"})
    client.post("/feedback", json={"product_id": "a", "rating": 1, "comment": "awful"})

    listed = client.get("/feedback")
    assert listed.status_code == 200
    assert listed.json()["count"] == 3

    filtered = client.get("/feedback", params={"product_id": "a"})
    assert filtered.json()["count"] == 2
    assert all(item["product_id"] == "a" for item in filtered.json()["items"])

    low = client.get("/feedback", params={"max_rating": 2})
    assert low.json()["count"] == 2

    limited = client.get("/feedback", params={"limit": 1})
    assert limited.json()["count"] == 1


def test_list_feedback_bad_rating_range(client):
    response = client.get("/feedback", params={"min_rating": 4, "max_rating": 2})
    assert response.status_code == 400


def test_list_feedback_limit_bounds(client):
    assert client.get("/feedback", params={"limit": 0}).status_code == 422
    assert client.get("/feedback", params={"limit": 500}).status_code == 422


def test_get_feedback_by_id_and_404(client):
    created = client.post("/feedback", json={"rating": 3, "comment": "ok"}).json()
    fetched = client.get("/feedback/" + created["feedback_id"])
    assert fetched.status_code == 200
    assert fetched.json()["feedback_id"] == created["feedback_id"]

    missing = client.get("/feedback/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Feedback not found"


def test_stats_empty(client):
    body = client.get("/feedback/stats").json()
    assert body["total_count"] == 0
    assert body["average_rating"] == 0.0
    assert body["rating_distribution"] == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}


def test_stats_with_items_and_product_scope(client):
    client.post("/feedback", json={"product_id": "a", "rating": 5, "comment": "great"})
    client.post("/feedback", json={"product_id": "a", "rating": 2, "comment": "meh"})
    client.post("/feedback", json={"product_id": "b", "rating": 4, "comment": "good"})

    overall = client.get("/feedback/stats").json()
    assert overall["total_count"] == 3
    assert overall["average_rating"] == 3.67
    assert overall["rating_distribution"]["5"] == 1

    scoped = client.get("/feedback/stats", params={"product_id": "a"}).json()
    assert scoped["product_id"] == "a"
    assert scoped["total_count"] == 2
    assert scoped["average_rating"] == 3.5


def test_health_ok_and_degraded(client, repo, notifier):
    body = client.get("/health").json()
    assert body == {"status": "ok", "dynamodb": True, "sns": True}

    repo.healthy = False
    notifier.healthy = False
    degraded = client.get("/health").json()
    assert degraded["status"] == "degraded"
    assert degraded["dynamodb"] is False
    assert degraded["sns"] is False


def test_repository_put_get_and_decimal_decoding():
    table = FakeTable()
    repository = storage.DynamoFeedbackRepository(table=table)
    repository.put_feedback(
        {
            "feedback_id": "f1",
            "product_id": "widget",
            "rating": 4,
            "comment": "good",
            "customer_email": None,
            "created_at": "2024-01-01T00:00:00Z",
            "alert_sent": False,
        }
    )
    assert "customer_email" not in table.written[0]

    table.items[0]["rating"] = Decimal("4")
    item = repository.get_feedback("f1")
    assert item["rating"] == 4
    assert isinstance(item["rating"], int)
    assert repository.get_feedback("nope") is None
    assert repository.ping() is True


def test_repository_query_used_for_product_and_scan_fallback():
    items = [
        {"feedback_id": "f1", "product_id": "a", "rating": Decimal("5"), "created_at": "2024-01-02T00:00:00Z"},
        {"feedback_id": "f2", "product_id": "b", "rating": Decimal("1"), "created_at": "2024-01-03T00:00:00Z"},
    ]
    table = FakeTable(items=items)
    repository = storage.DynamoFeedbackRepository(table=table)
    listed = repository.list_feedback(product_id="a")
    assert table.query_calls
    assert listed[0]["created_at"] == "2024-01-03T00:00:00Z"

    failing = FakeTable(items=items, fail_query=True)
    fallback_repo = storage.DynamoFeedbackRepository(table=failing)
    scoped = fallback_repo.list_feedback(product_id="a")
    assert failing.scan_calls
    assert [entry["feedback_id"] for entry in scoped] == ["f1"]

    ranged = fallback_repo.list_feedback(min_rating=2, max_rating=5)
    assert [entry["feedback_id"] for entry in ranged] == ["f1"]


def test_notifier_publishes_and_handles_failure():
    fake = FakeSnsClient()
    notifier = storage.SnsNotifier(client=fake)
    assert notifier.publish_low_rating({"feedback_id": "f1", "product_id": "a", "rating": 1}) is True
    assert fake.published[0]["TopicArn"].endswith("low-rating-alerts")
    assert "1 star(s)" in fake.published[0]["Subject"]
    assert notifier.ping() is True

    broken = storage.SnsNotifier(client=FakeSnsClient(fail=True))
    assert broken.publish_low_rating({"feedback_id": "f2", "product_id": "a", "rating": 2}) is False


def test_clients_use_endpoint_url_and_region(monkeypatch):
    captured = {}

    def fake_resource(name, **kwargs):
        captured["resource"] = (name, kwargs)
        return object()

    def fake_client(name, **kwargs):
        captured["client"] = (name, kwargs)
        return object()

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setattr(storage.boto3, "client", fake_client)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    storage.dynamodb_resource()
    storage.sns_client()

    assert captured["resource"][0] == "dynamodb"
    assert captured["resource"][1]["endpoint_url"] == "http://localhost:4566"
    assert captured["resource"][1]["region_name"] == "us-east-1"
    assert captured["client"][0] == "sns"
    assert captured["client"][1]["endpoint_url"] == "http://localhost:4566"

    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    storage.sns_client()
    assert captured["client"][1]["endpoint_url"] is None


def test_resource_name_defaults(monkeypatch):
    monkeypatch.delenv("FEEDBACK_TABLE_NAME", raising=False)
    monkeypatch.delenv("FEEDBACK_TOPIC_NAME", raising=False)
    assert storage.table_name() == "product-feedback"
    assert storage.topic_name() == "low-rating-alerts"
