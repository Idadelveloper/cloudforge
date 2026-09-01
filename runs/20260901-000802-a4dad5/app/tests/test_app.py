"""Offline tests for the event registration service.

Every AWS interaction is stubbed: the API tests inject in-memory
repository/publisher implementations, and the DynamoDB/SQS backends are
exercised against hand written fakes.
"""

import json

import pytest
from fastapi.testclient import TestClient

import storage
from app import app, get_publisher, get_repository
from storage import (
    DynamoRepository,
    EventFull,
    EventNotFound,
    InMemoryPublisher,
    InMemoryRepository,
    RegistrationNotFound,
    SqsPublisher,
)


@pytest.fixture()
def repo():
    return InMemoryRepository()


@pytest.fixture()
def publisher():
    return InMemoryPublisher()


@pytest.fixture()
def client(repo, publisher):
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_publisher] = lambda: publisher
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def make_event(client, title="PyConf", date="2030-05-01", capacity=2):
    response = client.post(
        "/events",
        json={"title": title, "date": date, "capacity": capacity},
    )
    assert response.status_code == 201
    return response.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["queue"] == "in-memory"


def test_create_event(client):
    body = make_event(client)
    assert body["title"] == "PyConf"
    assert body["capacity"] == 2
    assert body["registered_count"] == 0
    assert body["remaining_capacity"] == 2
    assert body["event_id"]


def test_create_event_rejects_bad_capacity(client):
    response = client.post("/events", json={"title": "x", "date": "2030-01-01", "capacity": 0})
    assert response.status_code == 422


def test_create_event_rejects_blank_title(client):
    response = client.post("/events", json={"title": "   ", "date": "2030-01-01", "capacity": 3})
    assert response.status_code == 422


def test_list_events(client):
    make_event(client, title="One")
    make_event(client, title="Two")
    response = client.get("/events")
    assert response.status_code == 200
    titles = sorted(item["title"] for item in response.json())
    assert titles == ["One", "Two"]


def test_get_event_and_404(client):
    event = make_event(client)
    response = client.get("/events/" + event["event_id"])
    assert response.status_code == 200
    assert response.json()["event_id"] == event["event_id"]

    missing = client.get("/events/does-not-exist")
    assert missing.status_code == 404


def test_registration_success_publishes_message(client, publisher):
    event = make_event(client, capacity=1)
    response = client.post(
        "/events/{0}/registrations".format(event["event_id"]),
        json={"attendee_name": "Ada", "attendee_email": "Ada@Example.com"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["attendee_email"] == "ada@example.com"
    assert body["status"] == "confirmed"

    assert len(publisher.messages) == 1
    message = publisher.messages[0]
    assert message["event_type"] == "registration.created"
    assert message["event_id"] == event["event_id"]
    assert message["registration_id"] == body["registration_id"]

    updated = client.get("/events/" + event["event_id"]).json()
    assert updated["registered_count"] == 1
    assert updated["remaining_capacity"] == 0


def test_registration_rejected_when_full(client):
    event = make_event(client, capacity=1)
    first = client.post(
        "/events/{0}/registrations".format(event["event_id"]),
        json={"attendee_name": "Ada", "attendee_email": "ada@example.com"},
    )
    assert first.status_code == 201

    second = client.post(
        "/events/{0}/registrations".format(event["event_id"]),
        json={"attendee_name": "Bob", "attendee_email": "bob@example.com"},
    )
    assert second.status_code == 409
    assert "full" in second.json()["detail"]


def test_duplicate_registration_rejected(client):
    event = make_event(client, capacity=5)
    payload = {"attendee_name": "Ada", "attendee_email": "ada@example.com"}
    assert client.post("/events/{0}/registrations".format(event["event_id"]), json=payload).status_code == 201
    duplicate = client.post("/events/{0}/registrations".format(event["event_id"]), json=payload)
    assert duplicate.status_code == 409
    assert "already registered" in duplicate.json()["detail"]


def test_registration_unknown_event(client):
    response = client.post(
        "/events/nope/registrations",
        json={"attendee_name": "Ada", "attendee_email": "ada@example.com"},
    )
    assert response.status_code == 404


def test_registration_invalid_email(client):
    event = make_event(client)
    response = client.post(
        "/events/{0}/registrations".format(event["event_id"]),
        json={"attendee_name": "Ada", "attendee_email": "not-an-email"},
    )
    assert response.status_code == 422


def test_registration_blank_name(client):
    event = make_event(client)
    response = client.post(
        "/events/{0}/registrations".format(event["event_id"]),
        json={"attendee_name": "   ", "attendee_email": "ada@example.com"},
    )
    assert response.status_code == 422


def test_list_registrations(client):
    event = make_event(client, capacity=3)
    for name, email in (("Ada", "ada@example.com"), ("Bob", "bob@example.com")):
        created = client.post(
            "/events/{0}/registrations".format(event["event_id"]),
            json={"attendee_name": name, "attendee_email": email},
        )
        assert created.status_code == 201

    response = client.get("/events/{0}/registrations".format(event["event_id"]))
    assert response.status_code == 200
    assert len(response.json()) == 2

    assert client.get("/events/unknown/registrations").status_code == 404


def test_get_registration_by_id(client):
    event = make_event(client)
    created = client.post(
        "/events/{0}/registrations".format(event["event_id"]),
        json={"attendee_name": "Ada", "attendee_email": "ada@example.com"},
    ).json()

    response = client.get("/registrations/" + created["registration_id"])
    assert response.status_code == 200
    assert response.json()["registration_id"] == created["registration_id"]

    assert client.get("/registrations/missing").status_code == 404


def test_publish_failure_does_not_fail_request(repo):
    class BrokenPublisher(InMemoryPublisher):
        def publish(self, message):
            raise RuntimeError("sqs unavailable")

    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_publisher] = BrokenPublisher
    with TestClient(app) as test_client:
        event = make_event(test_client)
        response = test_client.post(
            "/events/{0}/registrations".format(event["event_id"]),
            json={"attendee_name": "Ada", "attendee_email": "ada@example.com"},
        )
        assert response.status_code == 201
    app.dependency_overrides.clear()


def test_registration_write_failure_releases_capacity(publisher):
    class BrokenRepo(InMemoryRepository):
        def create_registration(self, event_id, attendee_name, attendee_email):
            raise RuntimeError("dynamodb unavailable")

    broken = BrokenRepo()
    app.dependency_overrides[get_repository] = lambda: broken
    app.dependency_overrides[get_publisher] = lambda: publisher
    with TestClient(app, raise_server_exceptions=False) as test_client:
        event = make_event(test_client)
        response = test_client.post(
            "/events/{0}/registrations".format(event["event_id"]),
            json={"attendee_name": "Ada", "attendee_email": "ada@example.com"},
        )
        assert response.status_code == 500
        refreshed = test_client.get("/events/" + event["event_id"]).json()
        assert refreshed["registered_count"] == 0
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# DynamoDB / SQS backends against fakes
# --------------------------------------------------------------------------


class ConditionalCheckFailed(Exception):
    pass


class FakeExceptions:
    ConditionalCheckFailedException = ConditionalCheckFailed


class FakeClient:
    exceptions = FakeExceptions()


class FakeMeta:
    client = FakeClient()


class FakeEventsTable:
    def __init__(self):
        self.meta = FakeMeta()
        self.items = {}
        self.fail_condition = False

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item["event_id"]] = dict(item)
        return {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["event_id"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}

    def update_item(self, **kwargs):
        if self.fail_condition:
            raise ConditionalCheckFailed("condition failed")
        item = self.items[kwargs["Key"]["event_id"]]
        item["registered_count"] = int(item["registered_count"]) + 1
        return {"Attributes": dict(item)}


class FakeRegistrationsTable:
    def __init__(self):
        self.meta = FakeMeta()
        self.items = []

    def put_item(self, **kwargs):
        self.items.append(dict(kwargs["Item"]))
        return {}

    def query(self, **kwargs):
        return {"Items": [dict(item) for item in self.items]}

    def scan(self, **kwargs):
        return {"Items": [dict(item) for item in self.items]}


class FakeDynamoResource:
    def __init__(self, tables):
        self.tables = tables

    def Table(self, name):  # noqa: N802 - mirrors the boto3 resource API
        return self.tables[name]


@pytest.fixture()
def dynamo_repo():
    tables = {"events": FakeEventsTable(), "registrations": FakeRegistrationsTable()}
    repository = DynamoRepository(
        events_table_name="events",
        registrations_table_name="registrations",
        dynamodb=FakeDynamoResource(tables),
    )
    return repository, tables


def test_dynamo_create_and_read_event(dynamo_repo):
    repository, _tables = dynamo_repo
    event = repository.create_event("Conf", "2030-01-01", 2)
    fetched = repository.get_event(event["event_id"])
    assert fetched["title"] == "Conf"
    assert fetched["capacity"] == 2
    assert [e["event_id"] for e in repository.list_events()] == [event["event_id"]]


def test_dynamo_get_event_missing(dynamo_repo):
    repository, _tables = dynamo_repo
    assert repository.find_event("nope") is None
    with pytest.raises(EventNotFound):
        repository.get_event("nope")


def test_dynamo_reserve_capacity(dynamo_repo):
    repository, _tables = dynamo_repo
    event = repository.create_event("Conf", "2030-01-01", 2)
    updated = repository.reserve_capacity(event["event_id"])
    assert updated["registered_count"] == 1


def test_dynamo_reserve_capacity_full(dynamo_repo):
    repository, tables = dynamo_repo
    event = repository.create_event("Conf", "2030-01-01", 1)
    tables["events"].fail_condition = True
    with pytest.raises(EventFull):
        repository.reserve_capacity(event["event_id"])


def test_dynamo_reserve_capacity_missing_event(dynamo_repo):
    repository, tables = dynamo_repo
    tables["events"].fail_condition = True
    with pytest.raises(EventNotFound):
        repository.reserve_capacity("unknown")


def test_dynamo_release_capacity_logs_on_condition_failure(dynamo_repo):
    repository, tables = dynamo_repo
    tables["events"].fail_condition = True
    repository.release_capacity("unknown")  # must not raise


def test_dynamo_registrations(dynamo_repo):
    repository, _tables = dynamo_repo
    event = repository.create_event("Conf", "2030-01-01", 5)
    registration = repository.create_registration(event["event_id"], "Ada", "Ada@Example.com")
    assert registration["attendee_email"] == "ada@example.com"

    listed = repository.list_registrations(event["event_id"])
    assert len(listed) == 1

    found = repository.find_registration_by_email(event["event_id"], "ada@example.com")
    assert found is not None
    assert repository.find_registration_by_email(event["event_id"], "bob@example.com") is None

    by_id = repository.get_registration(registration["registration_id"])
    assert by_id["attendee_name"] == "Ada"
    with pytest.raises(RegistrationNotFound):
        repository.get_registration("missing")


class FakeSqsClient:
    def __init__(self, queue_url="http://localhost:4566/000000000000/registration-events"):
        self.sent = []
        self.queue_url_value = queue_url
        self.get_queue_url_calls = []

    def get_queue_url(self, **kwargs):
        self.get_queue_url_calls.append(kwargs["QueueName"])
        return {"QueueUrl": self.queue_url_value}

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return {"MessageId": "1"}


def test_sqs_publisher_sends_json():
    fake = FakeSqsClient()
    pub = SqsPublisher(queue_url="http://queue/url", client=fake)
    pub.publish({"event_type": "registration.created", "registration_id": "r1"})
    assert fake.sent[0]["QueueUrl"] == "http://queue/url"
    assert json.loads(fake.sent[0]["MessageBody"])["registration_id"] == "r1"
    assert fake.get_queue_url_calls == []


def test_sqs_publisher_resolves_queue_url_from_name():
    fake = FakeSqsClient()
    pub = SqsPublisher(queue_url="", queue_name="registration-events", client=fake)
    pub.publish({"event_type": "registration.created"})
    assert fake.get_queue_url_calls == ["registration-events"]
    assert fake.sent[0]["QueueUrl"] == fake.queue_url_value


def test_client_factories_honour_endpoint_url(monkeypatch):
    captured = {}

    def fake_resource(service, **kwargs):
        captured["resource"] = (service, kwargs)
        return "resource"

    def fake_client(service, **kwargs):
        captured["client"] = (service, kwargs)
        return "client"

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setattr(storage.boto3, "client", fake_client)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    assert storage.dynamodb_resource() == "resource"
    assert storage.sqs_client() == "client"
    assert captured["resource"][0] == "dynamodb"
    assert captured["resource"][1]["endpoint_url"] == "http://localhost:4566"
    assert captured["resource"][1]["region_name"] == "us-east-1"
    assert captured["client"][0] == "sqs"
    assert captured["client"][1]["endpoint_url"] == "http://localhost:4566"


def test_endpoint_url_none_when_unset(monkeypatch):
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    assert storage.aws_endpoint_url() is None
