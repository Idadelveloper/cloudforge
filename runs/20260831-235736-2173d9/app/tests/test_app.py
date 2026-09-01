"""Offline tests for the event registration service.

Every AWS interaction is stubbed: the API tests use the in-memory repository
and publisher, while the DynamoDB/SQS layers are exercised against local fakes.
"""
import json
import os
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def repo():
    return storage.InMemoryRepository()


@pytest.fixture()
def publisher():
    return storage.InMemoryPublisher()


@pytest.fixture()
def client(repo, publisher):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    app_module.app.dependency_overrides[app_module.get_publisher] = lambda: publisher
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _create_event(client, title="PyConf", date="2025-06-01", capacity=2):
    response = client.post(
        "/events", json={"title": title, "date": date, "capacity": capacity}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "event-registration-service"
    assert body["dependencies"] == {"dynamodb": "ok", "sqs": "ok"}


def test_health_degraded(client):
    class _Down:
        def health(self):
            return False

    app_module.app.dependency_overrides[app_module.get_publisher] = _Down
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["sqs"] == "unavailable"


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def test_create_and_get_event(client):
    created = _create_event(client, title="  CloudForge Day ", capacity=5)
    assert created["title"] == "CloudForge Day"
    assert created["capacity"] == 5
    assert created["registered_count"] == 0
    assert created["remaining_capacity"] == 5
    assert created["event_id"]

    fetched = client.get(f"/events/{created['event_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["event_id"] == created["event_id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "date": "2025-06-01", "capacity": 3},
        {"title": "ok", "date": "not-a-date", "capacity": 3},
        {"title": "ok", "date": "2025-06-01", "capacity": 0},
        {"title": "ok", "date": "2025-06-01"},
    ],
)
def test_create_event_validation(client, payload):
    response = client.post("/events", json=payload)
    assert response.status_code == 422


def test_get_unknown_event_returns_404(client):
    response = client.get("/events/does-not-exist")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_list_events_with_pagination(client):
    for index in range(3):
        _create_event(client, title=f"event-{index}")

    first = client.get("/events", params={"limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert len(body["events"]) == 2
    assert body["next_cursor"]

    second = client.get("/events", params={"limit": 2, "cursor": body["next_cursor"]})
    assert second.status_code == 200
    tail = second.json()
    assert len(tail["events"]) == 1
    assert tail["next_cursor"] is None


def test_list_events_invalid_cursor(client):
    response = client.get("/events", params={"cursor": "!!!not-base64!!!"})
    assert response.status_code == 400


def test_list_events_limit_out_of_range(client):
    assert client.get("/events", params={"limit": 0}).status_code == 422
    assert client.get("/events", params={"limit": 5000}).status_code == 422


# --------------------------------------------------------------------------- #
# Registrations
# --------------------------------------------------------------------------- #
def test_register_attendee_publishes_message(client, publisher):
    event = _create_event(client, capacity=2)
    response = client.post(
        f"/events/{event['event_id']}/registrations",
        json={"attendee_name": "Ada", "attendee_email": "ada@example.com"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["queued"] is True
    assert body["remaining_capacity"] == 1

    assert len(publisher.messages) == 1
    message = publisher.messages[0]
    assert message["event_id"] == event["event_id"]
    assert message["attendee_email"] == "ada@example.com"
    assert message["event_title"] == event["title"]
    assert message["registration_id"] == body["registration_id"]

    detail = client.get(f"/events/{event['event_id']}").json()
    assert detail["registered_count"] == 1
    assert detail["remaining_capacity"] == 1


def test_duplicate_registration_returns_409(client):
    event = _create_event(client, capacity=5)
    payload = {"attendee_name": "Ada", "attendee_email": "ada@example.com"}
    assert client.post(f"/events/{event['event_id']}/registrations", json=payload).status_code == 201
    duplicate = client.post(f"/events/{event['event_id']}/registrations", json=payload)
    assert duplicate.status_code == 409
    assert "already registered" in duplicate.json()["detail"]


def test_registration_rejected_when_full(client):
    event = _create_event(client, capacity=1)
    first = client.post(
        f"/events/{event['event_id']}/registrations",
        json={"attendee_name": "Ada", "attendee_email": "ada@example.com"},
    )
    assert first.status_code == 201
    second = client.post(
        f"/events/{event['event_id']}/registrations",
        json={"attendee_name": "Grace", "attendee_email": "grace@example.com"},
    )
    assert second.status_code == 409
    assert "full capacity" in second.json()["detail"]


def test_register_unknown_event_returns_404(client):
    response = client.post(
        "/events/missing/registrations",
        json={"attendee_name": "Ada", "attendee_email": "ada@example.com"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"attendee_name": "Ada", "attendee_email": "not-an-email"},
        {"attendee_name": "   ", "attendee_email": "ada@example.com"},
        {"attendee_email": "ada@example.com"},
    ],
)
def test_registration_validation(client, payload):
    event = _create_event(client)
    response = client.post(f"/events/{event['event_id']}/registrations", json=payload)
    assert response.status_code == 422


def test_registration_succeeds_when_publish_fails(client, publisher):
    publisher.fail = True
    event = _create_event(client)
    response = client.post(
        f"/events/{event['event_id']}/registrations",
        json={"attendee_name": "Ada", "attendee_email": "ada@example.com"},
    )
    assert response.status_code == 201
    assert response.json()["queued"] is False
    assert publisher.messages == []


def test_list_registrations(client):
    event = _create_event(client, capacity=3)
    for name in ("ada", "grace"):
        client.post(
            f"/events/{event['event_id']}/registrations",
            json={"attendee_name": name, "attendee_email": f"{name}@example.com"},
        )
    response = client.get(f"/events/{event['event_id']}/registrations")
    assert response.status_code == 200
    body = response.json()
    assert len(body["registrations"]) == 2
    assert body["next_cursor"] is None

    paged = client.get(f"/events/{event['event_id']}/registrations", params={"limit": 1})
    assert len(paged.json()["registrations"]) == 1
    assert paged.json()["next_cursor"]


def test_list_registrations_unknown_event(client):
    assert client.get("/events/nope/registrations").status_code == 404


# --------------------------------------------------------------------------- #
# DynamoDB fakes
# --------------------------------------------------------------------------- #
class ConditionalCheckFailed(Exception):
    """Stand-in for the boto3 ConditionalCheckFailedException."""


class FakeTable:
    def __init__(self):
        self.items = {}
        self.rows = []

    def put_item(self, **kwargs):
        item = dict(kwargs["Item"])
        if "registration_id" in item:
            self.rows.append(item)
        else:
            self.items[item["event_id"]] = item
        return {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["event_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["event_id"])
        if item is None:
            raise ConditionalCheckFailed("event missing")
        if "- :one" in kwargs["UpdateExpression"]:
            if int(item["registered_count"]) <= 0:
                raise ConditionalCheckFailed("nothing to release")
            item["registered_count"] = int(item["registered_count"]) - 1
        else:
            if int(item["registered_count"]) >= int(item["capacity"]):
                raise ConditionalCheckFailed("event full")
            item["registered_count"] = int(item["registered_count"]) + 1
        return {"Attributes": dict(item)}

    def query(self, **kwargs):
        event_id = kwargs["ExpressionAttributeValues"][":eid"]
        matches = [dict(row) for row in self.rows if row["event_id"] == event_id]
        limit = kwargs.get("Limit")
        result = {"Items": matches[:limit] if limit else matches}
        if limit and len(matches) > limit:
            last = matches[limit - 1]
            result["LastEvaluatedKey"] = {
                "event_id": last["event_id"],
                "registration_id": last["registration_id"],
            }
        return result

    def scan(self, **kwargs):
        values = list(self.items.values())
        limit = int(kwargs.get("Limit", len(values) or 1))
        start = 0
        start_key = kwargs.get("ExclusiveStartKey")
        if start_key:
            ids = [value["event_id"] for value in values]
            start = ids.index(start_key["event_id"]) + 1
        page = values[start:start + limit]
        result = {"Items": [dict(item) for item in page]}
        if page and start + limit < len(values):
            result["LastEvaluatedKey"] = {"event_id": page[-1]["event_id"]}
        return result


class FakeDynamoResource:
    def __init__(self, describe_fails=False):
        self._tables = {}
        self._describe_fails = describe_fails
        exceptions = SimpleNamespace(ConditionalCheckFailedException=ConditionalCheckFailed)
        self.meta = SimpleNamespace(
            client=SimpleNamespace(exceptions=exceptions, describe_table=self._describe_table)
        )

    def _describe_table(self, TableName):
        if self._describe_fails:
            raise RuntimeError("table missing")
        return {"Table": {"TableName": TableName}}

    def Table(self, name):
        return self._tables.setdefault(name, FakeTable())


@pytest.fixture()
def dynamo_repo():
    return storage.DynamoDBRepository(
        dynamodb=FakeDynamoResource(),
        events_table="events",
        registrations_table="registrations",
    )


def test_dynamo_repo_event_lifecycle(dynamo_repo):
    created = dynamo_repo.create_event("Summit", "2025-09-09", 2)
    fetched = dynamo_repo.get_event(created["event_id"])
    assert fetched["title"] == "Summit"
    assert dynamo_repo.get_event("missing") is None

    events, cursor = dynamo_repo.list_events(limit=10)
    assert len(events) == 1
    assert cursor is None


def test_dynamo_repo_scan_pagination(dynamo_repo):
    for index in range(3):
        dynamo_repo.create_event(f"event-{index}", "2025-09-09", 5)
    page, cursor = dynamo_repo.list_events(limit=2)
    assert len(page) == 2
    assert cursor
    rest, next_cursor = dynamo_repo.list_events(limit=2, cursor=cursor)
    assert len(rest) == 1
    assert next_cursor is None


def test_dynamo_repo_registration_flow(dynamo_repo):
    event = dynamo_repo.create_event("Summit", "2025-09-09", 1)
    registration, updated = dynamo_repo.create_registration(
        event["event_id"], "Ada", "ada@example.com"
    )
    assert registration["status"] == "confirmed"
    assert updated["registered_count"] == 1

    with pytest.raises(storage.DuplicateRegistrationError):
        dynamo_repo.create_registration(event["event_id"], "Ada", "ADA@example.com")

    with pytest.raises(storage.EventFullError):
        dynamo_repo.create_registration(event["event_id"], "Grace", "grace@example.com")

    with pytest.raises(storage.EventNotFoundError):
        dynamo_repo.create_registration("missing", "Grace", "grace@example.com")

    rows, cursor = dynamo_repo.list_registrations(event["event_id"])
    assert len(rows) == 1
    assert cursor is None


def test_dynamo_repo_normalizes_decimals(dynamo_repo):
    event = dynamo_repo.create_event("Summit", "2025-09-09", 3)
    table = dynamo_repo.events_table
    table.items[event["event_id"]]["capacity"] = Decimal("3")
    table.items[event["event_id"]]["registered_count"] = Decimal("1")
    fetched = dynamo_repo.get_event(event["event_id"])
    assert fetched["capacity"] == 3
    assert isinstance(fetched["capacity"], int)
    assert fetched["registered_count"] == 1


def test_dynamo_repo_health():
    healthy = storage.DynamoDBRepository(dynamodb=FakeDynamoResource())
    assert healthy.health() is True
    broken = storage.DynamoDBRepository(dynamodb=FakeDynamoResource(describe_fails=True))
    assert broken.health() is False


def test_dynamo_repo_invalid_cursor(dynamo_repo):
    with pytest.raises(storage.InvalidCursorError):
        dynamo_repo.list_events(cursor="bogus-cursor")


# --------------------------------------------------------------------------- #
# SQS publisher
# --------------------------------------------------------------------------- #
class FakeSqsClient:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def get_queue_url(self, QueueName):
        if self.fail:
            raise RuntimeError("queue missing")
        return {"QueueUrl": "http://localhost:4566/000000000000/" + QueueName}

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append((QueueUrl, MessageBody))
        return {"MessageId": "fake-id"}


def test_sqs_publisher_sends_json():
    fake = FakeSqsClient()
    publisher = storage.SqsPublisher(client=fake, queue_name="registration-events")
    assert publisher.publish({"registration_id": "abc"}) is True
    assert publisher.health() is True
    url, body = fake.sent[0]
    assert url.endswith("registration-events")
    assert json.loads(body) == {"registration_id": "abc"}


def test_sqs_publisher_handles_failure():
    fake = FakeSqsClient(fail=True)
    publisher = storage.SqsPublisher(client=fake, queue_name="registration-events")
    assert publisher.publish({"registration_id": "abc"}) is False
    assert publisher.health() is False


def test_sqs_publisher_uses_explicit_queue_url():
    fake = FakeSqsClient(fail=True)
    publisher = storage.SqsPublisher(client=fake, queue_url="http://queue.local/q")
    assert publisher.publish({"a": 1}) is True
    assert fake.sent[0][0] == "http://queue.local/q"


# --------------------------------------------------------------------------- #
# AWS client factories / dependency wiring
# --------------------------------------------------------------------------- #
def test_clients_use_endpoint_url_env(monkeypatch):
    captured = {}

    def fake_factory(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return "client"

    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setattr(storage.boto3, "resource", fake_factory)
    monkeypatch.setattr(storage.boto3, "client", fake_factory)

    assert storage.dynamodb_resource() == "client"
    assert captured["service"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "eu-west-1"

    assert storage.sqs_client() == "client"
    assert captured["service"] == "sqs"


def test_clients_default_region_without_endpoint(monkeypatch):
    captured = {}

    def fake_resource(service, **kwargs):
        captured.update(kwargs)
        return "resource"

    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setattr(storage.boto3, "resource", fake_resource)

    storage.dynamodb_resource()
    assert captured["endpoint_url"] is None
    assert captured["region_name"] == "us-east-1"


def test_get_repository_and_publisher_are_cached(monkeypatch):
    monkeypatch.setattr(storage.boto3, "resource", lambda *a, **k: FakeDynamoResource())
    monkeypatch.setattr(storage.boto3, "client", lambda *a, **k: FakeSqsClient())
    app_module.get_repository.cache_clear()
    app_module.get_publisher.cache_clear()
    try:
        assert app_module.get_repository() is app_module.get_repository()
        assert app_module.get_publisher() is app_module.get_publisher()
    finally:
        app_module.get_repository.cache_clear()
        app_module.get_publisher.cache_clear()
