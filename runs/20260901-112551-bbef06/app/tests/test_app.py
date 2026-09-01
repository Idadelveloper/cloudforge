"""Offline tests for the IoT telemetry backend.

Every AWS interaction is replaced by an in-memory repository or a fake boto3
client, so the suite never touches the network or LocalStack.
"""

import os
import sys
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage as storage_module  # noqa: E402
from storage import (  # noqa: E402
    DynamoTelemetryRepository,
    InMemoryTelemetryRepository,
    StorageError,
)


@pytest.fixture()
def repo():
    return InMemoryTelemetryRepository()


@pytest.fixture()
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def register(client, device_id="dev-1", threshold=25.0):
    return client.post(
        "/devices",
        json={
            "device_id": device_id,
            "name": "sensor",
            "location": "lab",
            "threshold_celsius": threshold,
        },
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "iot_telemetry_backend"
    assert body["region"]


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #

def test_register_device_and_fetch(client, repo):
    response = register(client)
    assert response.status_code == 201
    device = response.json()["device"]
    assert device["device_id"] == "dev-1"
    assert device["threshold_celsius"] == 25.0
    assert device["created_at"] and device["updated_at"]
    assert "dev-1" in repo.devices

    fetched = client.get("/devices/dev-1")
    assert fetched.status_code == 200
    assert fetched.json()["device"]["location"] == "lab"


def test_register_device_uses_default_threshold(client):
    response = client.post("/devices", json={"device_id": "dev-default"})
    assert response.status_code == 201
    assert response.json()["device"]["threshold_celsius"] == app_module.default_threshold()


def test_register_duplicate_device_conflict(client):
    assert register(client).status_code == 201
    duplicate = register(client)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "device already registered"


def test_register_device_validation_error(client):
    response = client.post("/devices", json={"name": "missing id"})
    assert response.status_code == 422


def test_list_devices_with_limit(client):
    register(client, "dev-a")
    register(client, "dev-b")
    response = client.get("/devices")
    assert response.status_code == 200
    assert response.json()["count"] == 2

    limited = client.get("/devices", params={"limit": 1})
    assert limited.status_code == 200
    assert limited.json()["count"] == 1


def test_get_unknown_device_404(client):
    response = client.get("/devices/nope")
    assert response.status_code == 404


def test_set_threshold(client):
    register(client, "dev-t", 20.0)
    response = client.put("/devices/dev-t/threshold", json={"threshold_celsius": 41.5})
    assert response.status_code == 200
    assert response.json()["device"]["threshold_celsius"] == 41.5

    again = client.get("/devices/dev-t")
    assert again.json()["device"]["threshold_celsius"] == 41.5


def test_set_threshold_unknown_device_404(client):
    response = client.put("/devices/ghost/threshold", json={"threshold_celsius": 10})
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Readings ingest
# --------------------------------------------------------------------------- #

def test_ingest_reading_without_alert(client, repo):
    register(client, "dev-1", 30.0)
    response = client.post(
        "/readings",
        json={"device_id": "dev-1", "temperature_celsius": 21.5},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["alert_triggered"] is False
    assert body["alert_published"] is False
    assert body["reading"]["temperature_celsius"] == 21.5
    assert body["reading"]["date"] == body["reading"]["timestamp"][:10]
    assert repo.published_alerts == []


def test_ingest_reading_triggers_alert(client, repo):
    register(client, "dev-hot", 30.0)
    response = client.post(
        "/readings",
        json={
            "device_id": "dev-hot",
            "temperature_celsius": 44.25,
            "timestamp": "2024-05-01T12:30:00Z",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["alert_triggered"] is True
    assert body["alert_published"] is True
    assert body["alert_message_id"]
    assert len(repo.published_alerts) == 1
    alert = repo.published_alerts[0]
    assert alert["device_id"] == "dev-hot"
    assert alert["temperature_celsius"] == 44.25
    assert alert["threshold_celsius"] == 30.0
    assert "exceeds" in alert["message"]


def test_ingest_reading_at_threshold_does_not_alert(client, repo):
    register(client, "dev-edge", 30.0)
    response = client.post(
        "/readings",
        json={"device_id": "dev-edge", "temperature_celsius": 30.0},
    )
    assert response.status_code == 201
    assert response.json()["alert_triggered"] is False
    assert repo.published_alerts == []


def test_ingest_unregistered_device_404(client):
    response = client.post(
        "/readings",
        json={"device_id": "unknown", "temperature_celsius": 10},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "device not registered"


def test_ingest_invalid_timestamp_400(client):
    register(client, "dev-1")
    response = client.post(
        "/readings",
        json={"device_id": "dev-1", "temperature_celsius": 10, "timestamp": "not-a-time"},
    )
    assert response.status_code == 400


def test_ingest_normalizes_offset_timestamp(client):
    register(client, "dev-tz", 100.0)
    response = client.post(
        "/readings",
        json={
            "device_id": "dev-tz",
            "temperature_celsius": 5,
            "timestamp": "2024-05-01T02:00:00+02:00",
        },
    )
    assert response.status_code == 201
    assert response.json()["reading"]["timestamp"].startswith("2024-05-01T00:00:00")


# --------------------------------------------------------------------------- #
# Readings listing & stats
# --------------------------------------------------------------------------- #

def seed_readings(client):
    register(client, "dev-s", 100.0)
    for stamp, temp in [
        ("2024-05-01T01:00:00Z", 10.0),
        ("2024-05-01T13:00:00Z", 20.0),
        ("2024-05-01T23:59:59Z", 30.0),
        ("2024-05-02T01:00:00Z", 99.0),
    ]:
        response = client.post(
            "/readings",
            json={"device_id": "dev-s", "temperature_celsius": temp, "timestamp": stamp},
        )
        assert response.status_code == 201


def test_list_readings_and_range_filter(client):
    seed_readings(client)
    all_readings = client.get("/devices/dev-s/readings")
    assert all_readings.status_code == 200
    assert all_readings.json()["count"] == 4

    ranged = client.get(
        "/devices/dev-s/readings",
        params={"start": "2024-05-01T12:00:00Z", "end": "2024-05-02T00:00:00Z"},
    )
    assert ranged.status_code == 200
    body = ranged.json()
    assert body["count"] == 2
    assert [item["temperature_celsius"] for item in body["readings"]] == [20.0, 30.0]


def test_list_readings_unknown_device_404(client):
    assert client.get("/devices/ghost/readings").status_code == 404


def test_daily_stats(client):
    seed_readings(client)
    response = client.get("/devices/dev-s/stats/daily", params={"date": "2024-05-01"})
    assert response.status_code == 200
    body = response.json()
    assert body["date"] == "2024-05-01"
    assert body["count"] == 3
    assert body["min_celsius"] == 10.0
    assert body["max_celsius"] == 30.0
    assert body["avg_celsius"] == 20.0


def test_daily_stats_defaults_to_today(client):
    register(client, "dev-today", 50.0)
    client.post("/readings", json={"device_id": "dev-today", "temperature_celsius": 12.5})
    response = client.get("/devices/dev-today/stats/daily")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["avg_celsius"] == 12.5


def test_daily_stats_empty_day(client):
    register(client, "dev-empty")
    response = client.get("/devices/dev-empty/stats/daily", params={"date": "2020-01-01"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["min_celsius"] is None
    assert body["max_celsius"] is None
    assert body["avg_celsius"] is None


def test_daily_stats_invalid_date_400(client):
    register(client, "dev-bad-date")
    response = client.get("/devices/dev-bad-date/stats/daily", params={"date": "01-05-2024"})
    assert response.status_code == 400


def test_daily_stats_unknown_device_404(client):
    assert client.get("/devices/ghost/stats/daily").status_code == 404


# --------------------------------------------------------------------------- #
# Storage failure handling
# --------------------------------------------------------------------------- #

class BrokenRepository(InMemoryTelemetryRepository):
    def get_device(self, device_id):
        raise StorageError("dynamodb unreachable")


def test_storage_error_returns_503():
    app_module.app.dependency_overrides[app_module.get_repository] = BrokenRepository
    with TestClient(app_module.app) as test_client:
        response = test_client.get("/devices/dev-1")
    app_module.app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["detail"] == "storage backend unavailable"


def test_publish_failure_does_not_lose_reading(client, repo, monkeypatch):
    register(client, "dev-pub", 10.0)

    def boom(message):
        raise StorageError("sns down")

    monkeypatch.setattr(repo, "publish_alert", boom)
    response = client.post("/readings", json={"device_id": "dev-pub", "temperature_celsius": 55})
    assert response.status_code == 201
    body = response.json()
    assert body["alert_triggered"] is True
    assert body["alert_published"] is False
    assert repo.query_readings("dev-pub")


# --------------------------------------------------------------------------- #
# boto3 client factories
# --------------------------------------------------------------------------- #

def test_dynamodb_resource_honours_endpoint_env(monkeypatch):
    captured = {}

    def fake_resource(name, **kwargs):
        captured["name"] = name
        captured.update(kwargs)
        return "fake-resource"

    monkeypatch.setattr(storage_module.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    assert storage_module.dynamodb_resource() == "fake-resource"
    assert captured["name"] == "dynamodb"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["region_name"] == "us-east-1"


def test_sns_client_defaults_without_endpoint(monkeypatch):
    captured = {}

    def fake_client(name, **kwargs):
        captured["name"] = name
        captured.update(kwargs)
        return "fake-sns"

    monkeypatch.setattr(storage_module.boto3, "client", fake_client)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")

    assert storage_module.sns_client() == "fake-sns"
    assert captured["name"] == "sns"
    assert captured["endpoint_url"] is None
    assert captured["region_name"] == "eu-west-1"


def test_resource_name_defaults(monkeypatch):
    for var in ("DEVICES_TABLE", "READINGS_TABLE", "ALERTS_TOPIC_NAME"):
        monkeypatch.delenv(var, raising=False)
    assert storage_module.devices_table_name() == "iot-devices"
    assert storage_module.readings_table_name() == "iot-readings"
    assert storage_module.alerts_topic_name() == "iot-temperature-alerts"


# --------------------------------------------------------------------------- #
# DynamoDB repository against fake boto3 objects
# --------------------------------------------------------------------------- #

class FakeTable:
    def __init__(self, name):
        self.name = name
        self.items = {}
        self.queries = []

    @staticmethod
    def _key(key):
        return tuple(sorted((str(k), str(v)) for k, v in key.items()))

    def put_item(self, Item):  # noqa: N803 - boto3 API shape
        self.items[self._key({"device_id": Item["device_id"]})] = Item
        return {}

    def get_item(self, Key):  # noqa: N803
        item = self.items.get(self._key(Key))
        return {"Item": item} if item is not None else {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"Items": list(self.items.values())}

    def update_item(self, **kwargs):
        values = kwargs["ExpressionAttributeValues"]
        item = self.items.setdefault(
            self._key(kwargs["Key"]), dict(kwargs["Key"])
        )
        item["threshold_celsius"] = values[":t"]
        item["updated_at"] = values[":u"]
        return {"Attributes": item}


class FakeResource:
    def __init__(self):
        self.tables = {}

    def Table(self, name):  # noqa: N802 - boto3 API shape
        return self.tables.setdefault(name, FakeTable(name))


class FakeSns:
    def __init__(self):
        self.published = []
        self.created = []

    def create_topic(self, Name):  # noqa: N803
        self.created.append(Name)
        return {"TopicArn": "arn:aws:sns:us-east-1:000000000000:{0}".format(Name)}

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "msg-1"}


def test_dynamo_repository_device_roundtrip():
    resource = FakeResource()
    repo = DynamoTelemetryRepository(resource=resource, sns=FakeSns())

    assert repo.get_device("dev-x") is None
    repo.put_device(
        {
            "device_id": "dev-x",
            "name": "probe",
            "threshold_celsius": 27.5,
            "created_at": "2024-05-01T00:00:00Z",
            "updated_at": "2024-05-01T00:00:00Z",
        }
    )
    stored = resource.Table("iot-devices").items[(("device_id", "dev-x"),)]
    assert isinstance(stored["threshold_celsius"], Decimal)

    device = repo.get_device("dev-x")
    assert device["threshold_celsius"] == 27.5
    assert repo.list_devices(limit=10)[0]["device_id"] == "dev-x"

    updated = repo.update_threshold("dev-x", 31.0, "2024-05-02T00:00:00Z")
    assert updated["threshold_celsius"] == 31.0
    assert updated["updated_at"] == "2024-05-02T00:00:00Z"


def test_dynamo_repository_readings_and_alert(monkeypatch):
    monkeypatch.delenv("ALERTS_TOPIC_ARN", raising=False)
    monkeypatch.delenv("ALERTS_TOPIC_NAME", raising=False)
    resource = FakeResource()
    sns = FakeSns()
    repo = DynamoTelemetryRepository(resource=resource, sns=sns)

    repo.put_reading(
        {
            "device_id": "dev-y",
            "timestamp": "2024-05-01T10:00:00Z",
            "temperature_celsius": 40.5,
            "date": "2024-05-01",
            "alert_triggered": True,
        }
    )
    readings = repo.query_readings(
        "dev-y", start="2024-05-01T00", end="2024-05-01T24", limit=10
    )
    assert len(readings) == 1
    assert readings[0]["temperature_celsius"] == 40.5
    assert readings[0]["alert_triggered"] is True
    assert resource.Table("iot-readings").queries

    message_id = repo.publish_alert({"device_id": "dev-y", "message": "too hot"})
    assert message_id == "msg-1"
    assert sns.created == ["iot-temperature-alerts"]
    assert "dev-y" in sns.published[0]["Message"]
    assert sns.published[0]["TopicArn"].endswith("iot-temperature-alerts")


def test_dynamo_repository_uses_explicit_topic_arn():
    sns = FakeSns()
    repo = DynamoTelemetryRepository(
        resource=FakeResource(),
        sns=sns,
        topic_arn="arn:aws:sns:us-east-1:000000000000:custom-topic",
    )
    repo.publish_alert({"device_id": "dev-z"})
    assert sns.created == []
    assert sns.published[0]["TopicArn"].endswith("custom-topic")


def test_dynamo_repository_wraps_failures():
    class BoomTable(FakeTable):
        def get_item(self, Key):  # noqa: N803
            raise RuntimeError("network down")

    class BoomResource(FakeResource):
        def Table(self, name):  # noqa: N802
            return self.tables.setdefault(name, BoomTable(name))

    repo = DynamoTelemetryRepository(resource=BoomResource(), sns=FakeSns())
    with pytest.raises(StorageError):
        repo.get_device("dev-boom")


def test_build_repository_selects_memory_backend(monkeypatch):
    monkeypatch.setenv("REPOSITORY_BACKEND", "memory")
    assert isinstance(app_module.build_repository(), InMemoryTelemetryRepository)
    monkeypatch.setenv("REPOSITORY_BACKEND", "dynamodb")
    assert isinstance(app_module.build_repository(), DynamoTelemetryRepository)
