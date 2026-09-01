"""Offline tests for the notification hub API and storage layer."""

import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import storage


class FakeRepository:
    """In-memory stand-in for :class:`storage.NotificationRepository`."""

    def __init__(self):
        self.subscriptions = {}
        self.published = []
        self.queues = {
            "email": "http://localhost:4566/000000000000/notification-hub-email-queue",
            "webhook": "http://localhost:4566/000000000000/notification-hub-webhook-queue",
        }
        self.messages = {"email": [], "webhook": []}
        self.deleted_handles = []
        self.checks = {"sns": "ok", "sqs": "ok", "dynamodb": "ok"}
        self.fail_with = None
        self._counter = 0

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with

    def health(self):
        return dict(self.checks)

    def publish_event(self, event_type, subject, payload):
        self._maybe_fail()
        self._counter += 1
        event = {
            "event_id": "event-{}".format(self._counter),
            "event_type": event_type,
            "subject": subject,
            "payload": payload or {},
            "published_at": "2024-01-01T00:00:00Z",
            "sns_message_id": "sns-{}".format(self._counter),
        }
        self.published.append(event)
        return event

    def create_subscription(self, channel, target, event_types=None, active=True):
        self._maybe_fail()
        self._counter += 1
        item = {
            "subscription_id": "sub-{}".format(self._counter),
            "channel": channel,
            "target": target,
            "event_types": list(event_types or []),
            "active": bool(active),
            "created_at": "2024-01-01T00:00:0{}Z".format(self._counter % 10),
            "updated_at": "2024-01-01T00:00:0{}Z".format(self._counter % 10),
        }
        self.subscriptions[item["subscription_id"]] = item
        return dict(item)

    def get_subscription(self, subscription_id):
        self._maybe_fail()
        item = self.subscriptions.get(subscription_id)
        if not item:
            raise storage.NotFoundError("subscription '{}' not found".format(subscription_id))
        return dict(item)

    def list_subscriptions(self, channel=None, limit=100):
        self._maybe_fail()
        items = [dict(item) for item in self.subscriptions.values()]
        if channel:
            items = [item for item in items if item["channel"] == channel]
        items.sort(key=lambda item: item["subscription_id"])
        return items[:limit]

    def update_subscription(self, subscription_id, updates):
        self._maybe_fail()
        item = self.subscriptions.get(subscription_id)
        if not item:
            raise storage.NotFoundError("subscription '{}' not found".format(subscription_id))
        for key, value in updates.items():
            if key in ("target", "event_types", "active"):
                item[key] = value
        item["updated_at"] = "2024-01-02T00:00:00Z"
        return dict(item)

    def delete_subscription(self, subscription_id):
        self._maybe_fail()
        if subscription_id not in self.subscriptions:
            raise storage.NotFoundError("subscription '{}' not found".format(subscription_id))
        del self.subscriptions[subscription_id]

    def channel_queue_urls(self):
        self._maybe_fail()
        return dict(self.queues)

    def channel_stats(self, channel):
        self._maybe_fail()
        return {
            "channel": channel,
            "queue_url": self.queues[channel],
            "messages_available": 2,
            "messages_in_flight": 1,
            "messages_delayed": 0,
            "total_received": 3,
            "collected_at": "2024-01-01T00:00:00Z",
        }

    def all_channel_stats(self):
        return [self.channel_stats(channel) for channel in ("email", "webhook")]

    def receive_messages(self, channel, max_messages=10, wait_time_seconds=0, delete=False):
        self._maybe_fail()
        messages = self.messages.get(channel, [])[:max_messages]
        if delete:
            for message in messages:
                self.deleted_handles.append(message["receipt_handle"])
            self.messages[channel] = self.messages[channel][len(messages):]
        return [dict(message) for message in messages]


@pytest.fixture
def repo():
    return FakeRepository()


@pytest.fixture
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["sns"] == "ok"


def test_health_degraded(client, repo):
    repo.checks["sqs"] = "error: boom"
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_publish_event(client, repo):
    response = client.post(
        "/events",
        json={"event_type": "order.created", "subject": "New order", "payload": {"id": 7}},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["event_type"] == "order.created"
    assert body["sns_message_id"]
    assert body["payload"] == {"id": 7}
    assert len(repo.published) == 1


def test_publish_event_validation_error(client):
    response = client.post("/events", json={"payload": {}})
    assert response.status_code == 422


def test_publish_event_dependency_failure(client, repo):
    repo.fail_with = storage.DependencyError("sns down")
    response = client.post("/events", json={"event_type": "x"})
    assert response.status_code == 502
    assert "sns down" in response.json()["detail"]


def test_create_subscription_email(client):
    response = client.post(
        "/subscriptions",
        json={"channel": "email", "target": "ops@example.com", "event_types": ["order.created"]},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["channel"] == "email"
    assert body["target"] == "ops@example.com"
    assert body["active"] is True


def test_create_subscription_webhook(client):
    response = client.post(
        "/subscriptions",
        json={"channel": "webhook", "target": "https://example.com/hook"},
    )
    assert response.status_code == 201
    assert response.json()["event_types"] == []


def test_create_subscription_bad_target(client):
    response = client.post("/subscriptions", json={"channel": "email", "target": "not-an-email"})
    assert response.status_code == 422
    response = client.post("/subscriptions", json={"channel": "webhook", "target": "ftp://x"})
    assert response.status_code == 422


def test_create_subscription_unknown_channel(client):
    response = client.post("/subscriptions", json={"channel": "sms", "target": "x@y.z"})
    assert response.status_code == 422


def test_list_subscriptions_and_filter(client):
    client.post("/subscriptions", json={"channel": "email", "target": "a@example.com"})
    client.post("/subscriptions", json={"channel": "webhook", "target": "https://example.com/h"})
    response = client.get("/subscriptions")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    filtered = client.get("/subscriptions", params={"channel": "webhook"})
    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert filtered_body["count"] == 1
    assert filtered_body["subscriptions"][0]["channel"] == "webhook"


def test_get_subscription(client):
    created = client.post("/subscriptions", json={"channel": "email", "target": "a@example.com"}).json()
    response = client.get("/subscriptions/{}".format(created["subscription_id"]))
    assert response.status_code == 200
    assert response.json()["subscription_id"] == created["subscription_id"]


def test_get_subscription_missing(client):
    response = client.get("/subscriptions/nope")
    assert response.status_code == 404


def test_patch_subscription(client):
    created = client.post("/subscriptions", json={"channel": "email", "target": "a@example.com"}).json()
    response = client.patch(
        "/subscriptions/{}".format(created["subscription_id"]),
        json={"target": "b@example.com", "active": False, "event_types": ["a", "b"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["target"] == "b@example.com"
    assert body["active"] is False
    assert body["event_types"] == ["a", "b"]


def test_patch_subscription_invalid_target(client):
    created = client.post("/subscriptions", json={"channel": "email", "target": "a@example.com"}).json()
    response = client.patch(
        "/subscriptions/{}".format(created["subscription_id"]), json={"target": "bad"}
    )
    assert response.status_code == 422


def test_patch_subscription_empty_body(client):
    created = client.post("/subscriptions", json={"channel": "email", "target": "a@example.com"}).json()
    response = client.patch("/subscriptions/{}".format(created["subscription_id"]), json={})
    assert response.status_code == 422


def test_patch_subscription_missing(client):
    response = client.patch("/subscriptions/nope", json={"active": False})
    assert response.status_code == 404


def test_delete_subscription(client):
    created = client.post("/subscriptions", json={"channel": "email", "target": "a@example.com"}).json()
    response = client.delete("/subscriptions/{}".format(created["subscription_id"]))
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.delete("/subscriptions/{}".format(created["subscription_id"])).status_code == 404


def test_list_channels(client):
    response = client.get("/channels")
    assert response.status_code == 200
    channels = response.json()["channels"]
    assert [item["channel"] for item in channels] == ["email", "webhook"]
    assert channels[0]["queue_url"].endswith("notification-hub-email-queue")


def test_channel_stats(client):
    response = client.get("/channels/stats")
    assert response.status_code == 200
    rows = response.json()["channels"]
    assert len(rows) == 2
    assert rows[0]["total_received"] == 3


def test_channel_messages(client, repo):
    repo.messages["email"] = [
        {
            "message_id": "m1",
            "receipt_handle": "rh1",
            "body": {"event_type": "order.created"},
            "attributes": {"SentTimestamp": "1"},
        }
    ]
    response = client.get("/channels/email/messages")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["messages"][0]["body"]["event_type"] == "order.created"
    assert body["deleted"] is False


def test_channel_messages_with_delete(client, repo):
    repo.messages["webhook"] = [
        {"message_id": "m2", "receipt_handle": "rh2", "body": {}, "attributes": {}}
    ]
    response = client.get("/channels/webhook/messages", params={"delete": "true"})
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert repo.deleted_handles == ["rh2"]
    assert repo.messages["webhook"] == []


def test_channel_messages_unknown_channel(client):
    response = client.get("/channels/sms/messages")
    assert response.status_code == 422


# ----------------------------------------------------------------------
# storage layer tests with stubbed boto3 clients
# ----------------------------------------------------------------------
class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeSns:
    def __init__(self):
        self.published = []
        self.topics = [{"TopicArn": "arn:aws:sns:us-east-1:000000000000:notification-hub-events-topic"}]

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "msg-1"}

    def list_topics(self, **kwargs):
        return {"Topics": self.topics}

    def get_topic_attributes(self, **kwargs):
        return {"Attributes": {"TopicArn": kwargs["TopicArn"]}}


class FakeSqs:
    def __init__(self):
        self.deleted = []
        self.queues = {}

    def get_queue_url(self, QueueName):
        return {"QueueUrl": "http://localhost:4566/000000000000/" + QueueName}

    def get_queue_attributes(self, QueueUrl, AttributeNames):
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": "4",
                "ApproximateNumberOfMessagesNotVisible": "1",
                "ApproximateNumberOfMessagesDelayed": "0",
                "QueueArn": "arn:aws:sqs:us-east-1:000000000000:q",
            }
        }

    def receive_message(self, **kwargs):
        envelope = {
            "Type": "Notification",
            "Message": json.dumps({"event_type": "order.created", "payload": {"id": 1}}),
        }
        return {
            "Messages": [
                {
                    "MessageId": "m-1",
                    "ReceiptHandle": "rh-1",
                    "Body": json.dumps(envelope),
                    "Attributes": {"SentTimestamp": "1700000000000"},
                },
                {
                    "MessageId": "m-2",
                    "ReceiptHandle": "rh-2",
                    "Body": "plain text",
                    "Attributes": {},
                },
            ]
        }

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)
        return {}


class FakeTable:
    def __init__(self):
        self.items = {}
        self.loaded = False

    def load(self):
        self.loaded = True

    def put_item(self, Item):
        self.items[Item["subscription_id"]] = dict(Item)
        return {}

    def get_item(self, Key):
        item = self.items.get(Key["subscription_id"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        items = [dict(item) for item in self.items.values()]
        values = kwargs.get("ExpressionAttributeValues") or {}
        if ":c" in values:
            items = [item for item in items if item.get("channel") == values[":c"]]
        return {"Items": items}

    def query(self, **kwargs):
        channel = (kwargs.get("ExpressionAttributeValues") or {}).get(":c")
        items = [dict(item) for item in self.items.values() if item.get("channel") == channel]
        return {"Items": items}

    def update_item(self, **kwargs):
        key = kwargs["Key"]["subscription_id"]
        if key not in self.items:
            raise FakeClientError("ConditionalCheckFailedException")
        item = self.items[key]
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        for placeholder, attribute in names.items():
            item[attribute] = values[":" + placeholder[1:]]
        return {"Attributes": dict(item)}

    def delete_item(self, **kwargs):
        key = kwargs["Key"]["subscription_id"]
        if key not in self.items:
            raise FakeClientError("ConditionalCheckFailedException")
        del self.items[key]
        return {}


class FakeDynamoResource:
    def __init__(self, table):
        self._table = table
        self.requested = []

    def Table(self, name):  # noqa: N802 - boto3 API shape
        self.requested.append(name)
        return self._table


@pytest.fixture
def stubbed_storage(monkeypatch):
    sns = FakeSns()
    sqs = FakeSqs()
    table = FakeTable()
    monkeypatch.setattr(storage, "sns_client", lambda: sns)
    monkeypatch.setattr(storage, "sqs_client", lambda: sqs)
    monkeypatch.setattr(storage, "dynamodb_resource", lambda: FakeDynamoResource(table))
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    monkeypatch.delenv("EMAIL_QUEUE_URL", raising=False)
    monkeypatch.delenv("WEBHOOK_QUEUE_URL", raising=False)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    return storage.NotificationRepository(), sns, sqs, table


def test_storage_client_factories_read_environment(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    assert storage.region_name() == "eu-west-1"
    assert storage.endpoint_url() == "http://localhost:4566"
    monkeypatch.delenv("AWS_REGION")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert storage.region_name() == "us-east-1"


def test_storage_resolves_topic_and_queues(stubbed_storage):
    repo, _sns, _sqs, _table = stubbed_storage
    assert repo.topic_arn().endswith("notification-hub-events-topic")
    urls = repo.channel_queue_urls()
    assert urls["email"].endswith("notification-hub-email-queue")
    assert urls["webhook"].endswith("notification-hub-webhook-queue")


def test_storage_topic_arn_from_env(monkeypatch, stubbed_storage):
    repo, _sns, _sqs, _table = stubbed_storage
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:other")
    assert repo.topic_arn() == "arn:aws:sns:us-east-1:000000000000:other"


def test_storage_publish_event(stubbed_storage):
    repo, sns, _sqs, _table = stubbed_storage
    event = repo.publish_event("order.created", "subject", {"id": 1})
    assert event["sns_message_id"] == "msg-1"
    assert sns.published[0]["Subject"] == "subject"
    published = json.loads(sns.published[0]["Message"])
    assert published["event_type"] == "order.created"


def test_storage_subscription_crud(stubbed_storage):
    repo, _sns, _sqs, _table = stubbed_storage
    created = repo.create_subscription("email", "a@example.com", ["order.created"], True)
    sid = created["subscription_id"]
    assert repo.get_subscription(sid)["target"] == "a@example.com"
    repo.create_subscription("webhook", "https://example.com/hook")
    assert len(repo.list_subscriptions()) == 2
    assert len(repo.list_subscriptions("email")) == 1
    updated = repo.update_subscription(sid, {"target": "b@example.com", "active": False})
    assert updated["target"] == "b@example.com"
    assert updated["active"] is False
    repo.delete_subscription(sid)
    with pytest.raises(storage.NotFoundError):
        repo.get_subscription(sid)
    with pytest.raises(storage.NotFoundError):
        repo.delete_subscription(sid)
    with pytest.raises(storage.NotFoundError):
        repo.update_subscription(sid, {"active": True})


def test_storage_unknown_channel(stubbed_storage):
    repo, _sns, _sqs, _table = stubbed_storage
    with pytest.raises(storage.NotFoundError):
        repo.queue_url("sms")
    with pytest.raises(storage.NotFoundError):
        repo.create_subscription("sms", "x")
    with pytest.raises(storage.NotFoundError):
        repo.list_subscriptions("sms")


def test_storage_channel_stats_and_messages(stubbed_storage):
    repo, _sns, sqs, _table = stubbed_storage
    stats = repo.all_channel_stats()
    assert len(stats) == 2
    assert stats[0]["total_received"] == 5
    messages = repo.receive_messages("email", max_messages=5, delete=True)
    assert messages[0]["body"]["event_type"] == "order.created"
    assert messages[1]["body"] == {"raw": "plain text"}
    assert sqs.deleted == ["rh-1", "rh-2"]


def test_storage_health(stubbed_storage):
    repo, _sns, _sqs, table = stubbed_storage
    checks = repo.health()
    assert checks == {"sns": "ok", "sqs": "ok", "dynamodb": "ok"}
    assert table.loaded is True


def test_storage_health_reports_errors(monkeypatch, stubbed_storage):
    repo, sns, _sqs, _table = stubbed_storage

    def boom(**kwargs):
        raise RuntimeError("sns unreachable")

    monkeypatch.setattr(sns, "get_topic_attributes", boom)
    checks = repo.health()
    assert checks["sns"].startswith("error")


def test_decode_message_body_variants():
    envelope = {"Type": "Notification", "Message": json.dumps({"a": 1})}
    assert storage.decode_message_body(json.dumps(envelope)) == {"a": 1}
    envelope["Message"] = "hello"
    assert storage.decode_message_body(json.dumps(envelope)) == {"message": "hello"}
    assert storage.decode_message_body(json.dumps({"b": 2})) == {"b": 2}
    assert storage.decode_message_body("[1, 2]") == {"value": [1, 2]}
    assert storage.decode_message_body(None) == {"raw": None}


def test_normalise_subscription_defaults():
    record = storage.normalise_subscription({"subscription_id": "x", "event_types": "single"})
    assert record["event_types"] == ["single"]
    assert record["active"] is True
