"""Offline tests for the notification hub (no AWS/LocalStack required)."""
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage as storage_module  # noqa: E402


class FakeRepository:
    """In-memory stand-in for AwsNotificationRepository."""

    def __init__(self):
        self.subscriptions = {}
        self.published = []
        self.messages = {"email": [], "webhook": []}
        self.failing = False
        self.counter = 0

    def _check(self):
        if self.failing:
            raise RuntimeError("aws unavailable")

    def health(self):
        if self.failing:
            return {"sns": "error: RuntimeError", "sqs": "ok", "dynamodb": "ok"}
        return {"sns": "ok", "sqs": "ok", "dynamodb": "ok"}

    def create_subscription(self, channel, target, event_types=None):
        self._check()
        self.counter += 1
        subscription_id = "sub-{}".format(self.counter)
        item = {
            "subscription_id": subscription_id,
            "channel": channel,
            "target": target,
            "event_types": list(event_types or ["*"]),
            "active": True,
            "created_at": "2024-01-01T00:00:0{}Z".format(self.counter),
            "updated_at": "2024-01-01T00:00:0{}Z".format(self.counter),
            "sns_subscription_arn": "arn:aws:sns:us-east-1:000000000000:topic:{}".format(channel),
        }
        self.subscriptions[subscription_id] = item
        return item

    def list_subscriptions(self, channel=None, target=None):
        self._check()
        items = list(self.subscriptions.values())
        if channel:
            items = [item for item in items if item["channel"] == channel]
        if target:
            items = [item for item in items if item["target"] == target]
        return items

    def get_subscription(self, subscription_id):
        self._check()
        return self.subscriptions.get(subscription_id)

    def delete_subscription(self, subscription_id):
        self._check()
        return self.subscriptions.pop(subscription_id, None) is not None

    def publish_event(self, event_type, payload=None, channel=None, subject=None):
        self._check()
        record = {
            "event_type": event_type,
            "payload": payload or {},
            "channel": channel or "all",
            "subject": subject,
        }
        self.published.append(record)
        targets = [channel] if channel else ["email", "webhook"]
        for name in targets:
            self.messages[name].append(record)
        return {
            "message_id": "message-{}".format(len(self.published)),
            "topic_arn": "arn:aws:sns:us-east-1:000000000000:notification-hub-events",
            "published_at": "2024-01-01T00:00:00Z",
            "event": record,
        }

    def list_channels(self):
        self._check()
        return [
            {
                "channel": name,
                "queue_name": "notification-hub-{}-queue".format(name),
                "queue_url": "http://localhost:4566/000000000000/notification-hub-{}-queue".format(name),
                "queue_arn": "arn:aws:sqs:us-east-1:000000000000:notification-hub-{}-queue".format(name),
                "subscription_count": len(self.list_subscriptions(channel=name)),
            }
            for name in ("email", "webhook")
        ]

    def receive_messages(self, channel, max_messages=10, delete=False, wait_seconds=0):
        self._check()
        queued = self.messages[channel][:max_messages]
        if delete:
            self.messages[channel] = self.messages[channel][len(queued):]
        return [
            {
                "message_id": "m-{}".format(index),
                "receipt_handle": "rh-{}".format(index),
                "event_type": item["event_type"],
                "body": item,
                "sent_at": "2024-01-01T00:00:00Z",
            }
            for index, item in enumerate(queued)
        ]

    def stats(self):
        self._check()
        channels = []
        total = 0
        for name in ("email", "webhook"):
            subs = len(self.list_subscriptions(channel=name))
            total += subs
            channels.append(
                {
                    "channel": name,
                    "queue_url": "http://localhost:4566/000000000000/notification-hub-{}-queue".format(name),
                    "approximate_messages_received": len(self.messages[name]),
                    "approximate_number_of_messages": len(self.messages[name]),
                    "approximate_number_of_messages_not_visible": 0,
                    "approximate_number_of_messages_delayed": 0,
                    "subscription_count": subs,
                }
            )
        return {
            "topic_name": "notification-hub-events",
            "channels": channels,
            "total_subscriptions": total,
        }


@pytest.fixture()
def repo():
    return FakeRepository()


@pytest.fixture()
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
    repo.failing = True
    body = client.get("/health").json()
    assert body["status"] == "degraded"


def test_create_subscription(client):
    payload = {"channel": "email", "target": "ops@example.com", "event_types": ["order.created"]}
    response = client.post("/subscriptions", json=payload)
    assert response.status_code == 201
    sub = response.json()["subscription"]
    assert sub["channel"] == "email"
    assert sub["target"] == "ops@example.com"
    assert sub["event_types"] == ["order.created"]
    assert sub["active"] is True


def test_create_subscription_rejects_unknown_channel(client):
    response = client.post("/subscriptions", json={"channel": "pigeon", "target": "x"})
    assert response.status_code == 400
    assert "unsupported channel" in response.json()["detail"]


def test_create_subscription_rejects_blank_target(client):
    response = client.post("/subscriptions", json={"channel": "email", "target": "   "})
    assert response.status_code == 400


def test_create_subscription_validation_error(client):
    response = client.post("/subscriptions", json={"channel": "email"})
    assert response.status_code == 422


def test_list_subscriptions_and_filters(client):
    client.post("/subscriptions", json={"channel": "email", "target": "a@example.com"})
    client.post("/subscriptions", json={"channel": "webhook", "target": "https://hook"})

    all_subs = client.get("/subscriptions").json()
    assert all_subs["count"] == 2

    filtered = client.get("/subscriptions", params={"channel": "webhook"}).json()
    assert filtered["count"] == 1
    assert filtered["subscriptions"][0]["channel"] == "webhook"

    by_target = client.get("/subscriptions", params={"target": "a@example.com"}).json()
    assert by_target["count"] == 1

    assert client.get("/subscriptions", params={"channel": "sms"}).status_code == 400


def test_get_subscription(client):
    created = client.post(
        "/subscriptions", json={"channel": "email", "target": "a@example.com"}
    ).json()["subscription"]
    response = client.get("/subscriptions/{}".format(created["subscription_id"]))
    assert response.status_code == 200
    assert response.json()["subscription"]["subscription_id"] == created["subscription_id"]


def test_get_subscription_not_found(client):
    assert client.get("/subscriptions/missing").status_code == 404


def test_delete_subscription(client):
    created = client.post(
        "/subscriptions", json={"channel": "email", "target": "a@example.com"}
    ).json()["subscription"]
    sub_id = created["subscription_id"]
    response = client.delete("/subscriptions/{}".format(sub_id))
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.delete("/subscriptions/{}".format(sub_id)).status_code == 404


def test_publish_event(client, repo):
    response = client.post(
        "/events",
        json={"event_type": "order.created", "payload": {"order_id": "42"}},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["message_id"]
    assert body["topic_arn"].endswith("notification-hub-events")
    assert repo.published[0]["payload"] == {"order_id": "42"}


def test_publish_event_rejects_bad_channel(client):
    response = client.post("/events", json={"event_type": "x", "channel": "fax"})
    assert response.status_code == 400


def test_publish_event_rejects_empty_type(client):
    assert client.post("/events", json={"event_type": "  "}).status_code == 400


def test_list_channels(client):
    body = client.get("/channels").json()
    assert body["count"] == 2
    names = [item["channel"] for item in body["channels"]]
    assert names == ["email", "webhook"]
    assert body["channels"][0]["queue_url"].endswith("notification-hub-email-queue")


def test_channel_messages_peek_and_drain(client):
    client.post("/events", json={"event_type": "order.created", "channel": "email"})
    peek = client.get("/channels/email/messages").json()
    assert peek["count"] == 1
    assert peek["messages"][0]["event_type"] == "order.created"

    drained = client.get("/channels/email/messages", params={"delete": "true"}).json()
    assert drained["deleted"] is True
    assert client.get("/channels/email/messages").json()["count"] == 0


def test_channel_messages_bad_channel(client):
    assert client.get("/channels/carrier-pigeon/messages").status_code == 400


def test_channel_messages_bad_max(client):
    assert client.get("/channels/email/messages", params={"max_messages": 50}).status_code == 422


def test_stats(client):
    client.post("/subscriptions", json={"channel": "email", "target": "a@example.com"})
    client.post("/events", json={"event_type": "order.created"})
    body = client.get("/stats").json()
    assert body["total_subscriptions"] == 1
    assert body["generated_at"]
    by_channel = {item["channel"]: item for item in body["channels"]}
    assert by_channel["email"]["approximate_messages_received"] == 1
    assert by_channel["webhook"]["approximate_messages_received"] == 1


def test_backend_failure_maps_to_502(client, repo):
    repo.failing = True
    assert client.get("/subscriptions").status_code == 502
    assert client.get("/stats").status_code == 502


# --------------------------------------------------------------------------- storage layer


class FakeSns:
    def __init__(self):
        self.topic_arn = "arn:aws:sns:us-east-1:000000000000:notification-hub-events"
        self.published = []
        self.subscribed = []
        self.fail_topic_attributes = False

    def list_topics(self, **kwargs):
        return {"Topics": [{"TopicArn": self.topic_arn}]}

    def create_topic(self, **kwargs):
        return {"TopicArn": self.topic_arn}

    def get_topic_attributes(self, **kwargs):
        if self.fail_topic_attributes:
            raise RuntimeError("no topic")
        return {"Attributes": {"TopicArn": kwargs["TopicArn"]}}

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "msg-1"}

    def subscribe(self, **kwargs):
        self.subscribed.append(kwargs)
        return {"SubscriptionArn": "arn:aws:sns:us-east-1:000000000000:events:sub-1"}


class FakeSqs:
    def __init__(self):
        self.queues = {
            "notification-hub-email-queue": "http://localhost:4566/000000000000/email",
            "notification-hub-webhook-queue": "http://localhost:4566/000000000000/webhook",
        }
        self.messages = {url: [] for url in self.queues.values()}
        self.deleted = []

    def get_queue_url(self, **kwargs):
        return {"QueueUrl": self.queues[kwargs["QueueName"]]}

    def get_queue_attributes(self, **kwargs):
        url = kwargs["QueueUrl"]
        return {
            "Attributes": {
                "QueueArn": "arn:aws:sqs:us-east-1:000000000000:" + url.rsplit("/", 1)[-1],
                "ApproximateNumberOfMessages": str(len(self.messages[url])),
                "ApproximateNumberOfMessagesNotVisible": "1",
                "ApproximateNumberOfMessagesDelayed": "0",
            }
        }

    def receive_message(self, **kwargs):
        url = kwargs["QueueUrl"]
        limit = kwargs.get("MaxNumberOfMessages", 10)
        return {"Messages": self.messages[url][:limit]}

    def delete_message(self, **kwargs):
        url = kwargs["QueueUrl"]
        handle = kwargs["ReceiptHandle"]
        self.deleted.append(handle)
        self.messages[url] = [
            item for item in self.messages[url] if item.get("ReceiptHandle") != handle
        ]
        return {}


class FakeTable:
    table_status = "ACTIVE"

    def __init__(self):
        self.items = {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item["subscription_id"]] = dict(item)
        return {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]["subscription_id"]
        item = self.items.get(key)
        return {"Item": dict(item)} if item else {}

    def delete_item(self, **kwargs):
        self.items.pop(kwargs["Key"]["subscription_id"], None)
        return {}

    def scan(self, **kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}

    def query(self, **kwargs):
        raise RuntimeError("index not available")


class FakeDynamoResource:
    def __init__(self, table):
        self._table = table

    def Table(self, name):  # noqa: N802 - mirrors the boto3 resource API
        return self._table


@pytest.fixture()
def aws_stubs(monkeypatch):
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    sns = FakeSns()
    sqs = FakeSqs()
    table = FakeTable()
    monkeypatch.setattr(storage_module, "sns_client", lambda: sns)
    monkeypatch.setattr(storage_module, "sqs_client", lambda: sqs)
    monkeypatch.setattr(storage_module, "dynamodb_resource", lambda: FakeDynamoResource(table))
    return sns, sqs, table


def test_storage_topic_arn_lookup(aws_stubs):
    sns, _, _ = aws_stubs
    repository = storage_module.AwsNotificationRepository()
    assert repository.topic_arn() == sns.topic_arn


def test_storage_topic_arn_from_env(monkeypatch, aws_stubs):
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:preset")
    repository = storage_module.AwsNotificationRepository()
    assert repository.topic_arn().endswith("preset")


def test_storage_subscription_roundtrip(aws_stubs):
    sns, _, _ = aws_stubs
    repository = storage_module.AwsNotificationRepository()
    created = repository.create_subscription("email", "ops@example.com", ["order.*"])
    assert created["sns_subscription_arn"].endswith("sub-1")
    assert sns.subscribed[0]["Protocol"] == "sqs"

    repository.create_subscription("webhook", "https://hook.example.com")
    assert len(repository.list_subscriptions()) == 2
    assert len(repository.list_subscriptions(channel="email")) == 1
    assert repository.list_subscriptions(target="ops@example.com")[0]["channel"] == "email"

    fetched = repository.get_subscription(created["subscription_id"])
    assert fetched["target"] == "ops@example.com"
    assert repository.delete_subscription(created["subscription_id"]) is True
    assert repository.delete_subscription(created["subscription_id"]) is False


def test_storage_publish_event(aws_stubs):
    sns, _, _ = aws_stubs
    repository = storage_module.AwsNotificationRepository()
    result = repository.publish_event("order.created", {"id": 1}, channel="email", subject="hi")
    assert result["message_id"] == "msg-1"
    sent = sns.published[0]
    assert sent["Subject"] == "hi"
    assert json.loads(sent["Message"])["payload"] == {"id": 1}
    assert sent["MessageAttributes"]["channel"]["StringValue"] == "email"


def test_storage_receive_messages_unwraps_sns_envelope(aws_stubs):
    _, sqs, _ = aws_stubs
    url = sqs.queues["notification-hub-email-queue"]
    envelope = {
        "Type": "Notification",
        "Message": json.dumps({"event_type": "order.created", "payload": {"id": 7}}),
    }
    sqs.messages[url].append(
        {
            "MessageId": "m1",
            "ReceiptHandle": "rh1",
            "Body": json.dumps(envelope),
            "Attributes": {"SentTimestamp": "1700000000000"},
        }
    )
    repository = storage_module.AwsNotificationRepository()
    messages = repository.receive_messages("email", delete=True)
    assert len(messages) == 1
    assert messages[0]["event_type"] == "order.created"
    assert messages[0]["body"]["payload"] == {"id": 7}
    assert messages[0]["sent_at"].endswith("Z")
    assert sqs.deleted == ["rh1"]
    assert repository.receive_messages("email") == []


def test_storage_channels_and_stats(aws_stubs):
    _, sqs, _ = aws_stubs
    url = sqs.queues["notification-hub-webhook-queue"]
    sqs.messages[url].append({"MessageId": "m2", "ReceiptHandle": "rh2", "Body": "{}"})
    repository = storage_module.AwsNotificationRepository()
    repository.create_subscription("webhook", "https://hook.example.com")

    channels = repository.list_channels()
    assert [item["channel"] for item in channels] == ["email", "webhook"]
    assert channels[1]["subscription_count"] == 1

    stats = repository.stats()
    by_channel = {item["channel"]: item for item in stats["channels"]}
    assert by_channel["webhook"]["approximate_number_of_messages"] == 1
    assert by_channel["webhook"]["approximate_messages_received"] == 2
    assert stats["total_subscriptions"] == 1


def test_storage_health(aws_stubs):
    sns, _, _ = aws_stubs
    repository = storage_module.AwsNotificationRepository()
    assert repository.health() == {"sns": "ok", "sqs": "ok", "dynamodb": "ok"}
    sns.fail_topic_attributes = True
    assert repository.health()["sns"].startswith("error:")


def test_storage_helpers():
    assert storage_module.parse_message_body("not-json") == {"raw": "not-json"}
    assert storage_module.parse_message_body('{"a": 1}') == {"a": 1}
    assert storage_module.parse_message_body('[1, 2]') == {"message": [1, 2]}
    assert storage_module.epoch_ms_to_iso("nope").endswith("Z")
    assert storage_module.utcnow_iso().endswith("Z")


def test_client_factories_use_endpoint_env(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access")  # nosec - dummy local credential
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")  # nosec - dummy local credential
    assert storage_module.aws_endpoint_url() == "http://localhost:4566"
    assert storage_module.aws_region() == "us-east-1"
    client = storage_module.sqs_client()
    assert client.meta.region_name == "us-east-1"
