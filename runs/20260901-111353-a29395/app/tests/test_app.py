"""Offline tests for the IoT telemetry backend."""
import os
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage  # noqa: E402
from storage import DynamoTelemetryStore, InMemoryTelemetryStore  # noqa: E402

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture()
def store():
    """Fresh in-memory store per test."""
    return InMemoryTelemetryStore()


@pytest.fixture()
def client(store):
    """Test client with the AWS store replaced by the in-memory one."""
    app_module.app.dependency_overrides[app_module.get_store] = lambda: store
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def register(client, device_id="sensor-1", threshold=25.0):
    payload = {"device_id": device_id, "name": "Freezer", "location": "lab"}
    if threshold is not None:
        payload["threshold_celsius"] = threshold
    return client.post("/devices", json=payload)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "iot_telemetry_backend"


def test_register_device(client):
    response = register(client)
    assert response.status_code == 201
    body = response.json()
    assert body["device_id"] == "sensor-1"
    assert body["threshold_celsius"] == 25.0
    assert body["registered_at"].endswith("Z")


def test_register_device_applies_default_threshold(client):
    response = register(client, device_id="sensor-default", threshold=None)
    assert response.status_code == 201
    assert response.json()["threshold_celsius"] == 30.0


def test_register_device_rejects_empty_id(client):
    response = client.post("/devices", json={"device_id": "   "})
    assert response.status_code == 400


def test_register_device_conflict(client):
    assert register(client).status_code == 201
    duplicate = register(client)
    assert duplicate.status_code == 409


def test_list_devices(client):
    register(client, device_id="sensor-b")
    register(client, device_id="sensor-a")
    response = client.get("/devices")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [item["device_id"] for item in body["devices"]] == ["sensor-a", "sensor-b"]


def test_get_device_and_missing(client):
    register(client)
    ok = client.get("/devices/sensor-1")
    assert ok.status_code == 200
    assert ok.json()["location"] == "lab"
    missing = client.get("/devices/nope")
    assert missing.status_code == 404


def test_set_threshold(client):
    register(client)
    response = client.put("/devices/sensor-1/threshold", json={"threshold_celsius": 12.5})
    assert response.status_code == 200
    assert response.json()["threshold_celsius"] == 12.5
    assert client.get("/devices/sensor-1").json()["threshold_celsius"] == 12.5


def test_set_threshold_unknown_device(client):
    response = client.put("/devices/ghost/threshold", json={"threshold_celsius": 1.0})
    assert response.status_code == 404


def test_ingest_reading_below_threshold(client, store):
    register(client)
    response = client.post(
        "/readings",
        json={"device_id": "sensor-1", "temperature_celsius": 20.0, "timestamp": f"{TODAY}T08:00:00Z"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["alert_triggered"] is False
    assert body["alert_published"] is False
    assert body["reading"]["day"] == TODAY
    assert body["reading"]["threshold_at_ingest"] == 25.0
    assert store.published == []


def test_ingest_reading_triggers_alert(client, store):
    register(client)
    response = client.post(
        "/readings",
        json={"device_id": "sensor-1", "temperature_celsius": 42.0},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["alert_triggered"] is True
    assert body["alert_published"] is True
    assert len(store.published) == 1
    message = store.published[0]["message"]
    assert message["device_id"] == "sensor-1"
    assert message["temperature_celsius"] == 42.0
    assert message["threshold_celsius"] == 25.0
    assert "exceeds" in message["message"]


def test_ingest_reading_publish_failure_reported(client, store):
    register(client)
    store.publish_should_fail = True
    response = client.post("/readings", json={"device_id": "sensor-1", "temperature_celsius": 99.0})
    assert response.status_code == 201
    assert response.json()["alert_published"] is False


def test_ingest_reading_unknown_device(client):
    response = client.post("/readings", json={"device_id": "ghost", "temperature_celsius": 10.0})
    assert response.status_code == 404


def test_ingest_reading_invalid_timestamp(client):
    register(client)
    response = client.post(
        "/readings",
        json={"device_id": "sensor-1", "temperature_celsius": 10.0, "timestamp": "not-a-time"},
    )
    assert response.status_code == 400


def test_ingest_reading_validation_error(client):
    register(client)
    response = client.post("/readings", json={"device_id": "sensor-1"})
    assert response.status_code == 422


def test_list_readings_with_range(client):
    register(client)
    for hour, value in ((1, 10.0), (5, 20.0), (9, 30.0)):
        client.post(
            "/readings",
            json={
                "device_id": "sensor-1",
                "temperature_celsius": value,
                "timestamp": f"{TODAY}T0{hour}:00:00Z",
            },
        )
    all_readings = client.get("/devices/sensor-1/readings")
    assert all_readings.status_code == 200
    assert all_readings.json()["count"] == 3

    filtered = client.get(
        "/devices/sensor-1/readings",
        params={"start": f"{TODAY}T04:00:00Z", "end": f"{TODAY}T06:00:00Z"},
    )
    body = filtered.json()
    assert body["count"] == 1
    assert body["readings"][0]["temperature_celsius"] == 20.0


def test_list_readings_bad_range_and_missing_device(client):
    register(client)
    bad = client.get("/devices/sensor-1/readings", params={"start": "nope"})
    assert bad.status_code == 400
    missing = client.get("/devices/ghost/readings")
    assert missing.status_code == 404


def test_daily_stats(client):
    register(client, threshold=100.0)
    for hour, value in ((2, 10.0), (6, 20.0), (18, 15.0)):
        client.post(
            "/readings",
            json={
                "device_id": "sensor-1",
                "temperature_celsius": value,
                "timestamp": f"{TODAY}T{hour:02d}:00:00Z",
            },
        )
    response = client.get("/devices/sensor-1/stats/daily")
    assert response.status_code == 200
    body = response.json()
    assert body["date"] == TODAY
    assert body["count"] == 3
    assert body["min_celsius"] == 10.0
    assert body["max_celsius"] == 20.0
    assert body["avg_celsius"] == 15.0


def test_daily_stats_other_day_is_empty(client):
    register(client)
    client.post("/readings", json={"device_id": "sensor-1", "temperature_celsius": 5.0})
    response = client.get("/devices/sensor-1/stats/daily", params={"date": "1999-01-01"})
    body = response.json()
    assert body["count"] == 0
    assert body["min_celsius"] is None
    assert body["max_celsius"] is None
    assert body["avg_celsius"] is None


def test_daily_stats_invalid_date_and_missing_device(client):
    register(client)
    bad = client.get("/devices/sensor-1/stats/daily", params={"date": "01-01-2020"})
    assert bad.status_code == 400
    missing = client.get("/devices/ghost/stats/daily")
    assert missing.status_code == 404


# --- storage layer helpers -------------------------------------------------


class FakeTable:
    """Minimal stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, name):
        self.name = name
        self.items = {}

    @staticmethod
    def _key(item):
        return (item.get("device_id"), item.get("timestamp"))

    def put_item(self, Item):  # noqa: N803 - boto3 keyword
        self.items[self._key(Item)] = dict(Item)
        return {}

    def get_item(self, Key):  # noqa: N803 - boto3 keyword
        item = self.items.get(self._key(Key))
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}

    def query(self, **kwargs):
        assert "KeyConditionExpression" in kwargs
        return {"Items": [dict(item) for item in self.items.values()]}


class FakeResource:
    """Fake DynamoDB service resource returning :class:`FakeTable` handles."""

    def __init__(self):
        self.tables = {}

    def Table(self, name):  # noqa: N802 - boto3 API name
        return self.tables.setdefault(name, FakeTable(name))


class FakeSNS:
    """Fake SNS client capturing create_topic/publish calls."""

    def __init__(self):
        self.published = []
        self.topics = []

    def create_topic(self, Name):  # noqa: N803 - boto3 keyword
        self.topics.append(Name)
        return {"TopicArn": "arn:aws:sns:us-east-1:000000000000:" + Name}

    def publish(self, TopicArn, Subject, Message):  # noqa: N803 - boto3 keywords
        self.published.append({"arn": TopicArn, "subject": Subject, "message": Message})
        return {"MessageId": "fake-message-id"}


def test_dynamo_store_roundtrip():
    fake_sns = FakeSNS()
    dynamo_store = DynamoTelemetryStore(resource=FakeResource(), sns=fake_sns)

    dynamo_store.put_device(
        {"device_id": "d1", "threshold_celsius": 21.5, "name": None, "registered_at": "x"}
    )
    device = dynamo_store.get_device("d1")
    assert device is not None
    assert device["threshold_celsius"] == 21.5
    assert dynamo_store.get_device("missing") is None
    assert len(dynamo_store.list_devices()) == 1

    dynamo_store.put_reading(
        {
            "device_id": "d1",
            "timestamp": "2024-05-01T10:00:00.000000Z",
            "temperature_celsius": 33.25,
            "day": "2024-05-01",
            "alert_triggered": True,
            "threshold_at_ingest": 21.5,
        }
    )
    readings = dynamo_store.query_readings("d1", start="2024-05-01T00:00:00.000000Z",
                                           end="2024-05-01T23:59:59.999999Z", limit=10)
    assert len(readings) == 1
    assert readings[0]["temperature_celsius"] == 33.25
    assert readings[0]["alert_triggered"] is True

    assert dynamo_store.publish_alert("subject", {"device_id": "d1"}) is True
    assert fake_sns.published[0]["arn"].endswith(storage.alerts_topic_name())
    assert "d1" in fake_sns.published[0]["message"]


def test_dynamo_store_uses_configured_topic_arn(monkeypatch):
    monkeypatch.setenv("ALERTS_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:configured")
    fake_sns = FakeSNS()
    dynamo_store = DynamoTelemetryStore(resource=FakeResource(), sns=fake_sns)
    assert dynamo_store.topic_arn().endswith("configured")
    assert fake_sns.topics == []


class FakeBoto3:
    """Records the kwargs used to build AWS clients."""

    def __init__(self):
        self.calls = []

    def resource(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return "fake-resource"

    def client(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return "fake-client"


def test_clients_honour_endpoint_url(monkeypatch):
    fake = FakeBoto3()
    monkeypatch.setattr(storage, "boto3", fake)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    assert storage.dynamodb_resource() == "fake-resource"
    assert storage.sns_client() == "fake-client"
    for _, kwargs in fake.calls:
        assert kwargs["endpoint_url"] == "http://localhost:4566"
        assert kwargs["region_name"] == "us-east-1"


def test_clients_default_to_none_endpoint(monkeypatch):
    fake = FakeBoto3()
    monkeypatch.setattr(storage, "boto3", fake)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    storage.dynamodb_resource()
    assert fake.calls[0][1]["endpoint_url"] is None


def test_resource_names_from_environment(monkeypatch):
    monkeypatch.setenv("DEVICES_TABLE", "custom-devices")
    monkeypatch.setenv("READINGS_TABLE", "custom-readings")
    monkeypatch.setenv("ALERTS_TOPIC_NAME", "custom-topic")
    assert storage.devices_table_name() == "custom-devices"
    assert storage.readings_table_name() == "custom-readings"
    assert storage.alerts_topic_name() == "custom-topic"


def test_timestamp_helpers():
    parsed = app_module.parse_timestamp("2024-05-01T10:00:00Z")
    assert parsed.tzinfo is not None
    assert app_module.canonical_timestamp(parsed) == "2024-05-01T10:00:00.000000Z"
    naive = app_module.parse_timestamp("2024-05-01T10:00:00")
    assert naive.utcoffset().total_seconds() == 0
