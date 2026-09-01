"""Offline tests for the product feedback service.

Every AWS interaction is replaced by an in-memory fake, so the suite runs
without LocalStack, credentials or network access.
"""
import copy
import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import storage
from storage import (
    DynamoFeedbackRepository,
    FeedbackService,
    InMemoryFeedbackRepository,
    RecordingNotifier,
    SnsNotifier,
    StorageError,
)


class BrokenRepository(InMemoryFeedbackRepository):
    """Repository that always fails, to exercise the 503 paths."""

    def save(self, item):
        raise StorageError("dynamodb unavailable")

    def get(self, feedback_id):
        raise StorageError("dynamodb unavailable")

    def list_feedback(self, product_id=None, rating=None, limit=50):
        raise StorageError("dynamodb unavailable")


class FakeTable:
    """Minimal stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, name):
        self.name = name
        self.items = {}
        self.queries = []
        self.fail_scan = False
        self.fail_query = False

    def put_item(self, Item):
        self.items[Item["feedback_id"]] = copy.deepcopy(Item)
        return {}

    def get_item(self, Key):
        item = self.items.get(Key["feedback_id"])
        return {"Item": copy.deepcopy(item)} if item else {}

    def scan(self, **kwargs):
        if self.fail_scan:
            raise RuntimeError("scan boom")
        return {"Items": [copy.deepcopy(value) for value in self.items.values()]}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if self.fail_query:
            raise RuntimeError("query boom")
        return {"Items": [copy.deepcopy(value) for value in self.items.values()]}


class FakeDynamoResource:
    def __init__(self):
        self.tables = {}

    def Table(self, name):
        return self.tables.setdefault(name, FakeTable(name))


class FakeSnsClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.created = []
        self.published = []

    def create_topic(self, Name):
        self.created.append(Name)
        return {"TopicArn": "arn:aws:sns:us-east-1:000000000000:" + Name}

    def publish(self, **kwargs):
        if self.fail:
            raise RuntimeError("publish boom")
        self.published.append(kwargs)
        return {"MessageId": "msg-1"}


@pytest.fixture()
def repo():
    return InMemoryFeedbackRepository()


@pytest.fixture()
def notifier():
    return RecordingNotifier()


def _client_for(service):
    app_module.app.dependency_overrides[app_module.get_service] = lambda: service
    return TestClient(app_module.app)


@pytest.fixture()
def client(repo, notifier):
    service = FeedbackService(repo, notifier)
    with _client_for(service) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


@pytest.fixture()
def broken_client():
    service = FeedbackService(BrokenRepository(), RecordingNotifier())
    with _client_for(service) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _submit(client, product_id="widget-1", rating=5, comment="great", email=None):
    body = {"product_id": product_id, "rating": rating, "comment": comment}
    if email:
        body["customer_email"] = email
    return client.post("/feedback", json=body)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "product_feedback_service"
    assert payload["table"]
    assert payload["topic"]


def test_submit_high_rating_does_not_alert(client, notifier):
    response = _submit(client, rating=5, comment="loved it", email="a@example.com")
    assert response.status_code == 201
    body = response.json()
    assert body["feedback_id"]
    assert body["rating"] == 5
    assert body["comment"] == "loved it"
    assert body["customer_email"] == "a@example.com"
    assert body["alert_sent"] is False
    assert body["created_at"]
    assert notifier.messages == []


@pytest.mark.parametrize("rating", [1, 2])
def test_submit_low_rating_publishes_alert(client, notifier, rating):
    response = _submit(client, rating=rating, comment="broken on arrival")
    assert response.status_code == 201
    body = response.json()
    assert body["alert_sent"] is True
    assert len(notifier.messages) == 1
    alert = notifier.messages[0]
    assert alert["feedback_id"] == body["feedback_id"]
    assert alert["product_id"] == "widget-1"
    assert alert["rating"] == rating
    assert alert["comment"] == "broken on arrival"
    assert alert["created_at"] == body["created_at"]


def test_alert_failure_marks_alert_not_sent(repo):
    service = FeedbackService(repo, RecordingNotifier(succeed=False))
    with _client_for(service) as client:
        response = _submit(client, rating=1, comment="bad")
    app_module.app.dependency_overrides.clear()
    assert response.status_code == 201
    assert response.json()["alert_sent"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"product_id": "w", "rating": 6, "comment": "too high"},
        {"product_id": "w", "rating": 0, "comment": "too low"},
        {"product_id": "w", "rating": 3, "comment": ""},
        {"rating": 3, "comment": "missing product"},
        {"product_id": "w", "comment": "missing rating"},
    ],
)
def test_submit_validation_errors(client, body):
    assert client.post("/feedback", json=body).status_code == 422


def test_list_and_filter_feedback(client):
    _submit(client, product_id="widget-1", rating=5, comment="good")
    _submit(client, product_id="widget-1", rating=1, comment="bad")
    _submit(client, product_id="widget-2", rating=4, comment="ok")

    response = client.get("/feedback")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 3
    assert len(payload["items"]) == 3

    scoped = client.get("/feedback", params={"product_id": "widget-1"}).json()
    assert scoped["count"] == 2
    assert {item["product_id"] for item in scoped["items"]} == {"widget-1"}

    rated = client.get("/feedback", params={"rating": 1}).json()
    assert rated["count"] == 1
    assert rated["items"][0]["comment"] == "bad"

    limited = client.get("/feedback", params={"limit": 1}).json()
    assert limited["count"] == 1


def test_list_rejects_bad_query_params(client):
    assert client.get("/feedback", params={"rating": 9}).status_code == 422
    assert client.get("/feedback", params={"limit": 0}).status_code == 422


def test_get_single_feedback(client):
    created = _submit(client, rating=4, comment="fine").json()
    response = client.get("/feedback/" + created["feedback_id"])
    assert response.status_code == 200
    assert response.json() == created


def test_get_missing_feedback_returns_404(client):
    response = client.get("/feedback/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "feedback not found"


def test_average_rating(client):
    _submit(client, product_id="widget-1", rating=5, comment="a")
    _submit(client, product_id="widget-1", rating=2, comment="b")
    _submit(client, product_id="widget-2", rating=4, comment="c")

    overall = client.get("/feedback/stats/average").json()
    assert overall["count"] == 3
    assert overall["average_rating"] == pytest.approx(3.67, abs=0.01)
    assert overall["product_id"] is None
    assert overall["rating_breakdown"]["5"] == 1
    assert overall["rating_breakdown"]["2"] == 1
    assert overall["rating_breakdown"]["3"] == 0

    scoped = client.get(
        "/feedback/stats/average", params={"product_id": "widget-1"}
    ).json()
    assert scoped["product_id"] == "widget-1"
    assert scoped["count"] == 2
    assert scoped["average_rating"] == pytest.approx(3.5)


def test_average_rating_empty_store(client):
    payload = client.get("/feedback/stats/average").json()
    assert payload["count"] == 0
    assert payload["average_rating"] == 0.0
    assert payload["rating_breakdown"] == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}


def test_storage_failures_return_503(broken_client):
    assert _submit(broken_client).status_code == 503
    assert broken_client.get("/feedback").status_code == 503
    assert broken_client.get("/feedback/abc").status_code == 503
    assert broken_client.get("/feedback/stats/average").status_code == 503


def test_dynamo_repository_roundtrip():
    resource = FakeDynamoResource()
    repo = DynamoFeedbackRepository(table_name="t", index_name="idx", resource=resource)
    service = FeedbackService(repo, RecordingNotifier())

    created = service.create_feedback(
        {"product_id": "p1", "rating": 3, "comment": "meh", "customer_email": None}
    )
    fetched = repo.get(created["feedback_id"])
    assert fetched is not None
    assert fetched["product_id"] == "p1"
    assert fetched["rating"] == 3
    assert repo.get("nope") is None

    assert len(repo.list_feedback()) == 1
    scoped = repo.list_feedback(product_id="p1")
    assert len(scoped) == 1
    assert resource.Table("t").queries
    assert repo.list_feedback(product_id="other") == []
    assert repo.list_feedback(rating=5) == []


def test_dynamo_repository_query_falls_back_to_scan():
    resource = FakeDynamoResource()
    table = resource.Table("t")
    table.fail_query = True
    repo = DynamoFeedbackRepository(table_name="t", resource=resource)
    repo.save(
        {
            "feedback_id": "f1",
            "product_id": "p1",
            "rating": 2,
            "comment": "bad",
            "created_at": "2024-01-01T00:00:00Z",
            "alert_sent": True,
        }
    )
    items = repo.list_feedback(product_id="p1")
    assert len(items) == 1
    assert items[0]["alert_sent"] is True


def test_dynamo_repository_wraps_errors():
    resource = FakeDynamoResource()
    resource.Table("t").fail_scan = True
    repo = DynamoFeedbackRepository(table_name="t", resource=resource)
    with pytest.raises(StorageError):
        repo.list_feedback()


def test_sns_notifier_publishes_and_creates_topic():
    fake = FakeSnsClient()
    notifier = SnsNotifier(topic_arn="", topic_name="alerts", client=fake)
    alert = {
        "feedback_id": "f1",
        "product_id": "p1",
        "rating": 1,
        "comment": "terrible",
        "created_at": "2024-01-01T00:00:00Z",
    }
    assert notifier.publish_low_rating(alert) is True
    assert fake.created == ["alerts"]
    published = fake.published[0]
    assert published["TopicArn"].endswith("alerts")
    assert json.loads(published["Message"]) == alert
    assert "Low rating 1" in published["Subject"]


def test_sns_notifier_swallows_failures():
    notifier = SnsNotifier(
        topic_arn="arn:aws:sns:us-east-1:000000000000:alerts",
        client=FakeSnsClient(fail=True),
    )
    assert notifier.publish_low_rating({"feedback_id": "f", "rating": 1}) is False


def test_aws_clients_use_endpoint_url(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert storage.aws_endpoint_url() == "http://localhost:4566"
    assert storage.aws_region() == "us-east-1"
    sns = storage.sns_client()
    assert sns.meta.endpoint_url == "http://localhost:4566"
    dynamo = storage.dynamodb_resource()
    assert dynamo.meta.client.meta.endpoint_url == "http://localhost:4566"


def test_get_service_is_cached(monkeypatch):
    monkeypatch.setattr(app_module, "_service", None)
    first = app_module.get_service()
    second = app_module.get_service()
    assert first is second
    assert isinstance(first, FeedbackService)
    monkeypatch.setattr(app_module, "_service", None)
