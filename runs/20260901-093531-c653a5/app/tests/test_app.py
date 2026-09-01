"""Offline tests for the order-processing service.

All AWS interaction is replaced with in-memory fakes; no network or LocalStack
is required.
"""
import asyncio
import json
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from botocore.exceptions import ClientError

import storage
import worker
from app import app, get_publisher, get_repository
from storage import EventPublisher, OrderRepository, StorageError

try:  # pragma: no cover - depends on the installed test dependencies
    from fastapi.testclient import TestClient

    HAVE_TESTCLIENT = True
except Exception:  # pragma: no cover
    TestClient = None
    HAVE_TESTCLIENT = False


class _Response:
    """Minimal response object used by the fallback ASGI client."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return json.loads(self._body.decode("utf-8") or "null")


class _MiniClient:
    """Tiny stdlib ASGI client used when httpx/TestClient is unavailable."""

    def __init__(self, asgi_app):
        self.app = asgi_app

    def _request(self, method, path, params=None, body_json=None):
        query = urlencode(params or {})
        body = b"" if body_json is None else json.dumps(body_json).encode("utf-8")
        return asyncio.run(self._call(method, path, query, body))

    async def _call(self, method, path, query, body):
        chunks = []
        pending = [{"type": "http.request", "body": body, "more_body": False}]
        status = {"code": 500}

        async def receive():
            if pending:
                return pending.pop(0)
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query.encode("utf-8"),
            "root_path": "",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 5000),
            "server": ("testserver", 80),
        }
        await self.app(scope, receive, send)
        return _Response(status["code"], b"".join(chunks))

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, json=None):
        return self._request("POST", path, body_json=json)

    def patch(self, path, json=None):
        return self._request("PATCH", path, body_json=json)


def make_client():
    if HAVE_TESTCLIENT:
        return TestClient(app)
    return _MiniClient(app)


class FakeRepository(OrderRepository):
    """In-memory stand-in for the DynamoDB repository."""

    def __init__(self):
        self.orders = {}
        self.fail = False

    def create_order(self, order):
        if self.fail:
            raise StorageError("simulated dynamodb failure")
        self.orders[order["order_id"]] = dict(order)
        return dict(order)

    def get_order(self, order_id):
        item = self.orders.get(order_id)
        return dict(item) if item is not None else None

    def list_orders_by_customer(self, customer_id, status=None, limit=25):
        rows = [dict(o) for o in self.orders.values() if o["customer_id"] == customer_id]
        if status:
            rows = [row for row in rows if row["status"] == status]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return rows[:limit]

    def update_status(self, order_id, new_status, reason=None):
        item = self.orders.get(order_id)
        if item is None:
            return None
        item["status"] = new_status
        item["updated_at"] = storage.utc_now()
        if reason is not None:
            item["last_status_reason"] = reason
        return dict(item)


class FakePublisher(EventPublisher):
    """Records outbound messages instead of calling SQS/SNS."""

    def __init__(self):
        self.messages = []
        self.events = []
        self.fail = False

    def send_fulfilment_message(self, message):
        if self.fail:
            raise StorageError("simulated sqs failure")
        self.messages.append(message)
        return "msg-1"

    def publish_status_event(self, event):
        if self.fail:
            raise StorageError("simulated sns failure")
        self.events.append(event)
        return "sns-1"


ORDER_PAYLOAD = {
    "customer_id": "cust-1",
    "items": [
        {"sku": "WIDGET-1", "description": "Blue widget", "quantity": 2, "unit_price": 10.5},
        {"sku": "WIDGET-2", "quantity": 1, "unit_price": 4.25},
    ],
    "currency": "usd",
    "shipping_address": "1 Test Street",
    "notes": "leave at door",
}


@pytest.fixture()
def ctx():
    repo = FakeRepository()
    pub = FakePublisher()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_publisher] = lambda: pub
    yield make_client(), repo, pub
    app.dependency_overrides.clear()


def test_health(ctx):
    client, _repo, _pub = ctx
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["aws"]["region"]
    assert body["aws"]["orders_table"] == storage.orders_table_name()


def test_create_order_writes_and_publishes(ctx):
    client, repo, pub = ctx
    response = client.post("/orders", json=ORDER_PAYLOAD)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["currency"] == "USD"
    assert body["total_amount"] == pytest.approx(25.25)
    assert body["order_id"] in repo.orders
    assert len(pub.messages) == 1
    assert pub.messages[0]["order_id"] == body["order_id"]
    assert len(pub.events) == 1
    assert pub.events[0]["new_status"] == "PENDING"


def test_create_order_validation_error(ctx):
    client, _repo, _pub = ctx
    response = client.post("/orders", json={"customer_id": "c", "items": []})
    assert response.status_code == 422


def test_create_order_storage_failure_returns_502(ctx):
    client, repo, _pub = ctx
    repo.fail = True
    response = client.post("/orders", json=ORDER_PAYLOAD)
    assert response.status_code == 502


def test_create_order_survives_messaging_failure(ctx):
    client, repo, pub = ctx
    pub.fail = True
    response = client.post("/orders", json=ORDER_PAYLOAD)
    assert response.status_code == 201
    assert response.json()["order_id"] in repo.orders


def test_get_order(ctx):
    client, _repo, _pub = ctx
    created = client.post("/orders", json=ORDER_PAYLOAD).json()
    response = client.get("/orders/" + created["order_id"])
    assert response.status_code == 200
    assert response.json()["order_id"] == created["order_id"]


def test_get_order_not_found(ctx):
    client, _repo, _pub = ctx
    response = client.get("/orders/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "Order not found"


def test_list_orders_by_customer(ctx):
    client, _repo, _pub = ctx
    client.post("/orders", json=ORDER_PAYLOAD)
    other = dict(ORDER_PAYLOAD)
    other["customer_id"] = "cust-2"
    client.post("/orders", json=other)

    response = client.get("/orders", params={"customer_id": "cust-1"})
    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == "cust-1"
    assert body["count"] == 1
    assert body["orders"][0]["customer_id"] == "cust-1"

    filtered = client.get("/orders", params={"customer_id": "cust-1", "status": "FULFILLED"})
    assert filtered.json()["count"] == 0

    empty = client.get("/orders", params={"customer_id": "nobody"})
    assert empty.json()["count"] == 0


def test_list_orders_requires_customer_id(ctx):
    client, _repo, _pub = ctx
    assert client.get("/orders").status_code == 422


def test_list_orders_rejects_unknown_status(ctx):
    client, _repo, _pub = ctx
    response = client.get("/orders", params={"customer_id": "cust-1", "status": "WEIRD"})
    assert response.status_code == 400


def test_update_status_publishes_event(ctx):
    client, _repo, pub = ctx
    created = client.post("/orders", json=ORDER_PAYLOAD).json()
    response = client.patch(
        "/orders/{0}/status".format(created["order_id"]),
        json={"status": "PROCESSING", "reason": "picked by warehouse"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "PROCESSING"
    last_event = pub.events[-1]
    assert last_event["previous_status"] == "PENDING"
    assert last_event["new_status"] == "PROCESSING"
    assert last_event["reason"] == "picked by warehouse"


def test_update_status_not_found(ctx):
    client, _repo, _pub = ctx
    response = client.patch("/orders/missing/status", json={"status": "FAILED"})
    assert response.status_code == 404


def test_update_status_invalid_value(ctx):
    client, _repo, _pub = ctx
    created = client.post("/orders", json=ORDER_PAYLOAD).json()
    response = client.patch(
        "/orders/{0}/status".format(created["order_id"]),
        json={"status": "NOT-A-STATUS"},
    )
    assert response.status_code == 422


class FakeTable:
    """Very small DynamoDB table double."""

    def __init__(self):
        self.items = {}
        self.last_query = None

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item["order_id"]] = item
        return {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["order_id"])
        return {"Item": item} if item is not None else {}

    def query(self, **kwargs):
        self.last_query = kwargs
        return {"Items": list(self.items.values())}

    def update_item(self, **kwargs):
        key = kwargs["Key"]["order_id"]
        item = self.items.get(key)
        if item is None:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "missing"}},
                "UpdateItem",
            )
        item["status"] = kwargs["ExpressionAttributeValues"][":st"]
        item["updated_at"] = kwargs["ExpressionAttributeValues"][":updated"]
        return {"Attributes": item}


class FakeSqs:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def get_queue_url(self, **kwargs):
        return {"QueueUrl": "http://localstack:4566/000000000000/" + kwargs["QueueName"]}

    def send_message(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append(kwargs)
        return {"MessageId": "sqs-1"}


class FakeSns:
    def __init__(self):
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "sns-1"}


def _sample_order():
    now = storage.utc_now()
    return {
        "order_id": "o-1",
        "customer_id": "cust-1",
        "status": "PENDING",
        "items": [{"sku": "A", "description": None, "quantity": 2, "unit_price": 10.5}],
        "total_amount": 21.0,
        "currency": "USD",
        "shipping_address": None,
        "notes": None,
        "created_at": now,
        "updated_at": now,
    }


def test_dynamo_repository_roundtrip():
    table = FakeTable()
    repo = storage.DynamoOrderRepository(table=table, index_name="customer_id-created_at-index")
    repo.create_order(_sample_order())

    stored = table.items["o-1"]
    assert isinstance(stored["items"][0]["unit_price"], Decimal)

    fetched = repo.get_order("o-1")
    assert fetched["items"][0]["unit_price"] == 10.5
    assert repo.get_order("nope") is None

    rows = repo.list_orders_by_customer("cust-1", status="PENDING", limit=5)
    assert len(rows) == 1
    assert table.last_query["IndexName"] == "customer_id-created_at-index"
    assert table.last_query["Limit"] == 5
    assert "FilterExpression" in table.last_query

    updated = repo.update_status("o-1", "FULFILLED", reason="shipped")
    assert updated["status"] == "FULFILLED"
    assert repo.update_status("missing", "FAILED") is None


def test_publisher_sends_and_publishes(monkeypatch):
    monkeypatch.delenv("ORDER_QUEUE_URL", raising=False)
    sqs = FakeSqs()
    sns = FakeSns()
    publisher = storage.AwsEventPublisher(
        sqs=sqs,
        sns=sns,
        topic="arn:aws:sns:us-east-1:000000000000:order-status-topic",
    )
    assert publisher.send_fulfilment_message({"order_id": "o-1"}) == "sqs-1"
    assert json.loads(sqs.sent[0]["MessageBody"])["order_id"] == "o-1"
    assert "order-fulfilment-queue" in sqs.sent[0]["QueueUrl"]

    assert publisher.publish_status_event({"order_id": "o-1", "new_status": "FULFILLED"}) == "sns-1"
    assert sns.published[0]["TopicArn"].endswith("order-status-topic")


def test_publisher_skips_sns_without_topic(monkeypatch):
    monkeypatch.delenv("ORDER_STATUS_TOPIC_ARN", raising=False)
    publisher = storage.AwsEventPublisher(sqs=FakeSqs(), sns=FakeSns())
    assert publisher.publish_status_event({"order_id": "o-1", "new_status": "PENDING"}) is None


def test_publisher_wraps_client_errors(monkeypatch):
    monkeypatch.setenv("ORDER_QUEUE_URL", "http://localstack:4566/000000000000/order-fulfilment-queue")
    error = ClientError({"Error": {"Code": "AWS.SimpleQueueService.NonExistentQueue"}}, "SendMessage")
    publisher = storage.AwsEventPublisher(sqs=FakeSqs(error=error), sns=FakeSns())
    with pytest.raises(StorageError):
        publisher.send_fulfilment_message({"order_id": "o-1"})


def test_aws_clients_use_endpoint_override(monkeypatch):
    captured = {}

    def fake_factory(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return object()

    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setattr(storage.boto3, "resource", fake_factory)
    monkeypatch.setattr(storage.boto3, "client", fake_factory)

    storage.dynamodb_resource()
    assert captured["service"] == "dynamodb"
    assert captured["region_name"] == "us-east-1"
    assert captured["endpoint_url"] == "http://localhost:4566"

    storage.sqs_client()
    assert captured["service"] == "sqs"
    storage.sns_client()
    assert captured["service"] == "sns"


def test_worker_fulfils_orders():
    repo = FakeRepository()
    pub = FakePublisher()
    order = _sample_order()
    repo.create_order(order)
    event = {
        "Records": [
            {"messageId": "m-1", "body": json.dumps({"order_id": "o-1"})},
            {"messageId": "m-2", "body": json.dumps({"order_id": "missing"})},
            {"messageId": "m-3", "body": "not-json"},
        ]
    }
    result = worker.handler(event, None, repo=repo, publisher=pub)
    assert result["processed"] == ["o-1"]
    failures = [item["itemIdentifier"] for item in result["batchItemFailures"]]
    assert failures == ["m-2", "m-3"]
    assert repo.orders["o-1"]["status"] == "FULFILLED"
    assert pub.events[-1]["new_status"] == "FULFILLED"
