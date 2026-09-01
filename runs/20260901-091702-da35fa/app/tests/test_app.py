"""Offline tests for the order processing service.

Every AWS interaction is replaced either by the in-memory implementations from
``storage.py`` (injected through FastAPI dependency overrides) or by local fake
boto3 clients, so the suite never touches the network or LocalStack.
"""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

import app as app_module
import storage
import worker as worker_module

try:  # pragma: no cover - depends on the installed starlette/httpx combination
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - fall back to the stdlib ASGI caller
    TestClient = None


class _SimpleResponse:
    """Minimal response object mirroring the parts of httpx we use."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    @property
    def text(self):
        return self._body.decode("utf-8")

    def json(self):
        return json.loads(self.text or "null")


class SimpleASGIClient:
    """Tiny stdlib ASGI client used when fastapi's TestClient is unavailable."""

    def __init__(self, application):
        self.application = application

    def get(self, path, params=None):
        return self._request("GET", path, None, params)

    def post(self, path, json=None, params=None):
        return self._request("POST", path, json, params)

    def patch(self, path, json=None, params=None):
        return self._request("PATCH", path, json, params)

    def _request(self, method, path, body_obj, params):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._call(method, path, body_obj, params))
        finally:
            loop.close()

    async def _call(self, method, path, body_obj, params):
        payload = b"" if body_obj is None else json.dumps(body_obj).encode("utf-8")
        query = urlencode(params or {}, doseq=True).encode("ascii")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query,
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        state = {"sent": False}
        messages = []

        async def receive():
            if not state["sent"]:
                state["sent"] = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await self.application(scope, receive, send)

        status_code = 500
        body = b""
        for message in messages:
            if message["type"] == "http.response.start":
                status_code = message["status"]
            elif message["type"] == "http.response.body":
                body += message.get("body", b"")
        return _SimpleResponse(status_code, body)


def _make_client(application):
    if TestClient is not None:
        return TestClient(application)
    return SimpleASGIClient(application)


def _override(repo=None, queue=None, notifier=None):
    repo = repo or storage.InMemoryOrderRepository()
    queue = queue or storage.InMemoryFulfillmentQueue()
    notifier = notifier or storage.InMemoryOrderNotifier()
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    app_module.app.dependency_overrides[app_module.get_queue] = lambda: queue
    app_module.app.dependency_overrides[app_module.get_notifier] = lambda: notifier
    return SimpleNamespace(
        client=_make_client(app_module.app),
        repo=repo,
        queue=queue,
        notifier=notifier,
    )


@pytest.fixture()
def ctx():
    context = _override()
    yield context
    app_module.app.dependency_overrides.clear()


def _payload(customer_id="cust-1", **overrides):
    body = {
        "customer_id": customer_id,
        "items": [
            {"sku": "SKU-1", "name": "Widget", "quantity": 2, "unit_price": 9.99},
            {"sku": "SKU-2", "quantity": 1, "unit_price": 5.0},
        ],
        "currency": "usd",
        "shipping_address": "1 Main St",
        "notes": "leave at door",
    }
    body.update(overrides)
    return body


def _create(ctx, **kwargs):
    response = ctx.client.post("/orders", json=_payload(**kwargs))
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
def test_health_reports_all_dependencies_ok(ctx):
    response = ctx.client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "order_processing_service"
    assert body["dependencies"] == {"dynamodb": "ok", "sqs": "ok", "sns": "ok"}


def test_health_degraded_when_dependency_fails():
    context = _override(
        repo=storage.InMemoryOrderRepository(fail=True),
        queue=storage.InMemoryFulfillmentQueue(fail=True),
    )
    try:
        body = context.client.get("/health").json()
        assert body["status"] == "degraded"
        assert body["dependencies"]["dynamodb"].startswith("error")
        assert body["dependencies"]["sqs"].startswith("error")
        assert body["dependencies"]["sns"] == "ok"
    finally:
        app_module.app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# create order
# --------------------------------------------------------------------------- #
def test_create_order_persists_and_enqueues(ctx):
    order = _create(ctx)

    assert order["order_id"]
    assert order["customer_id"] == "cust-1"
    assert order["status"] == "QUEUED"
    assert order["currency"] == "USD"
    assert order["total_amount"] == pytest.approx(24.98)
    assert len(order["items"]) == 2
    assert order["created_at"] and order["updated_at"]

    assert order["order_id"] in ctx.repo.orders
    assert len(ctx.queue.messages) == 1
    message = ctx.queue.messages[0]
    assert message["order_id"] == order["order_id"]
    assert message["event_type"] == "order.created"
    assert message["status"] == "PENDING"
    assert message["total_amount"] == pytest.approx(24.98)


def test_create_order_rejects_empty_items(ctx):
    response = ctx.client.post("/orders", json=_payload(items=[]))
    assert response.status_code == 422


def test_create_order_rejects_invalid_quantity(ctx):
    bad = _payload(items=[{"sku": "SKU-1", "quantity": 0, "unit_price": 1.0}])
    assert ctx.client.post("/orders", json=bad).status_code == 422


def test_create_order_requires_customer_id(ctx):
    body = _payload()
    body.pop("customer_id")
    assert ctx.client.post("/orders", json=body).status_code == 422


def test_create_order_returns_502_when_queue_unavailable():
    context = _override(queue=storage.InMemoryFulfillmentQueue(fail=True))
    try:
        response = context.client.post("/orders", json=_payload())
        assert response.status_code == 502
        stored = list(context.repo.orders.values())
        assert len(stored) == 1
        assert stored[0]["status"] == "PENDING"
    finally:
        app_module.app.dependency_overrides.clear()


def test_create_order_returns_502_when_table_unavailable():
    context = _override(repo=storage.InMemoryOrderRepository(fail=True))
    try:
        response = context.client.post("/orders", json=_payload())
        assert response.status_code == 502
        assert context.queue.messages == []
    finally:
        app_module.app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# read endpoints
# --------------------------------------------------------------------------- #
def test_get_order(ctx):
    order = _create(ctx)
    response = ctx.client.get("/orders/%s" % order["order_id"])
    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == order["order_id"]
    assert body["shipping_address"] == "1 Main St"
    assert body["notes"] == "leave at door"


def test_get_order_not_found(ctx):
    response = ctx.client.get("/orders/does-not-exist")
    assert response.status_code == 404


def test_get_order_status(ctx):
    order = _create(ctx)
    response = ctx.client.get("/orders/%s/status" % order["order_id"])
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"order_id", "status", "updated_at"}
    assert body["status"] == "QUEUED"


def test_get_order_status_not_found(ctx):
    assert ctx.client.get("/orders/nope/status").status_code == 404


# --------------------------------------------------------------------------- #
# status update / SNS
# --------------------------------------------------------------------------- #
def test_patch_status_publishes_sns_event(ctx):
    order = _create(ctx)
    response = ctx.client.patch(
        "/orders/%s/status" % order["order_id"],
        json={"status": "fulfilled", "reason": "picked and packed"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "FULFILLED"
    assert body["previous_status"] == "QUEUED"
    assert body["notified"] is True

    assert len(ctx.notifier.published) == 1
    event = ctx.notifier.published[0]
    assert event["order_id"] == order["order_id"]
    assert event["new_status"] == "FULFILLED"
    assert event["previous_status"] == "QUEUED"
    assert event["reason"] == "picked and packed"
    assert event["changed_at"]

    assert ctx.repo.orders[order["order_id"]]["status"] == "FULFILLED"


def test_patch_status_rejects_unknown_status(ctx):
    order = _create(ctx)
    response = ctx.client.patch("/orders/%s/status" % order["order_id"], json={"status": "TELEPORTED"})
    assert response.status_code == 400
    assert ctx.notifier.published == []


def test_patch_status_unknown_order(ctx):
    response = ctx.client.patch("/orders/missing/status", json={"status": "CANCELLED"})
    assert response.status_code == 404


def test_patch_status_reports_notification_failure():
    context = _override(notifier=storage.InMemoryOrderNotifier(fail=True))
    try:
        order = _create(context)
        response = context.client.patch(
            "/orders/%s/status" % order["order_id"], json={"status": "PROCESSING"}
        )
        assert response.status_code == 200
        assert response.json()["notified"] is False
        assert context.repo.orders[order["order_id"]]["status"] == "PROCESSING"
    finally:
        app_module.app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #
def test_list_orders_by_customer(ctx):
    _create(ctx, customer_id="cust-a")
    _create(ctx, customer_id="cust-a")
    _create(ctx, customer_id="cust-b")

    response = ctx.client.get("/orders", params={"customer_id": "cust-a"})
    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == "cust-a"
    assert body["count"] == 2
    assert body["next_token"] is None
    assert {order["customer_id"] for order in body["orders"]} == {"cust-a"}


def test_list_orders_requires_customer_id(ctx):
    assert ctx.client.get("/orders").status_code == 422


def test_list_orders_status_filter(ctx):
    first = _create(ctx, customer_id="cust-c")
    _create(ctx, customer_id="cust-c")
    ctx.client.patch("/orders/%s/status" % first["order_id"], json={"status": "FULFILLED"})

    body = ctx.client.get("/orders", params={"customer_id": "cust-c", "status": "FULFILLED"}).json()
    assert body["count"] == 1
    assert body["orders"][0]["order_id"] == first["order_id"]

    bad = ctx.client.get("/orders", params={"customer_id": "cust-c", "status": "NOPE"})
    assert bad.status_code == 400


def test_list_orders_pagination(ctx):
    for _ in range(3):
        _create(ctx, customer_id="cust-p")

    first = ctx.client.get("/orders", params={"customer_id": "cust-p", "limit": 2}).json()
    assert first["count"] == 2
    assert first["next_token"]

    second = ctx.client.get(
        "/orders", params={"customer_id": "cust-p", "limit": 2, "next_token": first["next_token"]}
    ).json()
    assert second["count"] == 1
    assert second["next_token"] is None


def test_list_orders_invalid_token(ctx):
    response = ctx.client.get("/orders", params={"customer_id": "cust-p", "next_token": "!!!!"})
    assert response.status_code == 400


def test_customer_alias_route(ctx):
    _create(ctx, customer_id="cust-alias")
    response = ctx.client.get("/customers/cust-alias/orders")
    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == "cust-alias"
    assert body["count"] == 1


def test_list_orders_returns_502_when_backend_fails():
    context = _override(repo=storage.InMemoryOrderRepository(fail=True))
    try:
        response = context.client.get("/orders", params={"customer_id": "x"})
        assert response.status_code == 502
    finally:
        app_module.app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #
def test_worker_fulfils_orders(ctx):
    order = _create(ctx)
    event = {"Records": [{"messageId": "m1", "body": json.dumps(ctx.queue.messages[0])}]}

    result = worker_module.handler(event, None, repository=ctx.repo, notifier=ctx.notifier)

    assert result == {"processed": 1, "batchItemFailures": []}
    assert ctx.repo.orders[order["order_id"]]["status"] == "FULFILLED"
    assert ctx.notifier.published[0]["new_status"] == "FULFILLED"


def test_worker_reports_batch_item_failures(ctx):
    event = {
        "Records": [
            {"messageId": "bad-json", "body": "{not json"},
            {"messageId": "missing-id", "body": json.dumps({"foo": "bar"})},
            {"messageId": "unknown-order", "body": json.dumps({"order_id": "nope"})},
        ]
    }
    result = worker_module.handler(event, None, repository=ctx.repo, notifier=ctx.notifier)
    assert result["processed"] == 0
    assert [item["itemIdentifier"] for item in result["batchItemFailures"]] == [
        "bad-json",
        "missing-id",
        "unknown-order",
    ]


# --------------------------------------------------------------------------- #
# helpers / boto3 wiring
# --------------------------------------------------------------------------- #
def test_token_round_trip():
    token = storage.encode_token({"order_id": "abc", "created_at": "2024-01-01T00:00:00Z"})
    assert storage.decode_token(token)["order_id"] == "abc"
    with pytest.raises(storage.InvalidTokenError):
        storage.decode_token("@@@@")
    with pytest.raises(storage.InvalidTokenError):
        storage.decode_token("")


def test_decimal_conversion_helpers():
    item = storage.to_dynamo({"price": 1.5, "qty": 2, "skip": None, "nested": [{"x": 0.25}]})
    assert item["price"] == Decimal("1.5")
    assert "skip" not in item
    plain = storage.from_dynamo(item)
    assert plain["price"] == 1.5
    assert plain["qty"] == 2
    assert plain["nested"][0]["x"] == 0.25
    assert storage.quantize_amount("2.005") == Decimal("2.01")


def test_client_factories_use_endpoint_and_region(monkeypatch):
    captured = {}

    def fake_resource(name, **kwargs):
        captured["resource:" + name] = kwargs
        return "resource-" + name

    def fake_client(name, **kwargs):
        captured["client:" + name] = kwargs
        return "client-" + name

    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setattr(storage.boto3, "client", fake_client)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    assert storage.dynamodb_resource() == "resource-dynamodb"
    assert storage.sqs_client() == "client-sqs"
    assert storage.sns_client() == "client-sns"

    for key in ("resource:dynamodb", "client:sqs", "client:sns"):
        assert captured[key]["endpoint_url"] == "http://localhost:4566"
        assert captured[key]["region_name"] == "us-east-1"


def test_resource_names_from_environment(monkeypatch):
    monkeypatch.setenv("ORDERS_TABLE_NAME", "my-orders")
    monkeypatch.setenv("ORDER_QUEUE_NAME", "my-queue")
    monkeypatch.setenv("ORDER_STATUS_TOPIC_NAME", "my-topic")
    assert storage.orders_table_name() == "my-orders"
    assert storage.fulfillment_queue_name() == "my-queue"
    assert storage.status_topic_name() == "my-topic"

    monkeypatch.delenv("ORDERS_TABLE_NAME", raising=False)
    monkeypatch.delenv("ORDERS_TABLE", raising=False)
    monkeypatch.delenv("ORDER_QUEUE_NAME", raising=False)
    monkeypatch.delenv("FULFILLMENT_QUEUE_NAME", raising=False)
    monkeypatch.delenv("ORDER_STATUS_TOPIC_NAME", raising=False)
    assert storage.orders_table_name() == storage.DEFAULT_TABLE_NAME
    assert storage.fulfillment_queue_name() == storage.DEFAULT_QUEUE_NAME
    assert storage.status_topic_name() == storage.DEFAULT_TOPIC_NAME


class _FakeTable:
    """Stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self):
        self.items = {}
        self.loaded = False
        self.last_query = None

    def put_item(self, Item=None, **kwargs):
        self.items[Item["order_id"]] = dict(Item)
        return {}

    def get_item(self, Key=None, **kwargs):
        item = self.items.get(Key["order_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key=None, ExpressionAttributeValues=None, **kwargs):
        item = self.items.get(Key["order_id"])
        if item is None:
            raise storage.ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "missing"}},
                "UpdateItem",
            )
        item["status"] = ExpressionAttributeValues[":status"]
        item["updated_at"] = ExpressionAttributeValues[":updated_at"]
        if ":reason" in (ExpressionAttributeValues or {}):
            item["status_reason"] = ExpressionAttributeValues[":reason"]
        return {"Attributes": dict(item)}

    def query(self, **kwargs):
        self.last_query = kwargs
        items = list(self.items.values())
        return {"Items": items, "LastEvaluatedKey": {"order_id": items[-1]["order_id"]} if items else None}

    def load(self):
        self.loaded = True


class _FakeDynamoResource:
    def __init__(self, table):
        self._table = table

    def Table(self, name):  # noqa: N802 - mirrors the boto3 API
        self._table.name = name
        return self._table


def test_dynamo_repository_against_fake_table():
    table = _FakeTable()
    repo = storage.DynamoOrderRepository(
        table_name="orders", index_name="customer_id-created_at-index",
        resource=_FakeDynamoResource(table),
    )

    repo.put_order(
        {
            "order_id": "o-1",
            "customer_id": "c-1",
            "total_amount": 12.5,
            "status": "PENDING",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "notes": None,
        }
    )
    stored = repo.get_order("o-1")
    assert stored["total_amount"] == 12.5
    assert "notes" not in stored
    assert repo.get_order("missing") is None

    updated = repo.update_status("o-1", "FULFILLED", reason="done")
    assert updated["status"] == "FULFILLED"
    assert updated["status_reason"] == "done"

    with pytest.raises(storage.NotFoundError):
        repo.update_status("missing", "FULFILLED")

    orders, token = repo.list_by_customer("c-1", limit=10, status="FULFILLED")
    assert [order["order_id"] for order in orders] == ["o-1"]
    assert token
    assert table.last_query["IndexName"] == "customer_id-created_at-index"
    assert table.last_query["ScanIndexForward"] is False
    assert "FilterExpression" in table.last_query

    assert repo.health()["table"] == "orders"
    assert table.loaded is True


class _FakeSqsClient:
    def __init__(self):
        self.sent = []

    def get_queue_url(self, QueueName=None, **kwargs):
        return {"QueueUrl": "http://localhost:4566/000000000000/%s" % QueueName}

    def send_message(self, QueueUrl=None, MessageBody=None, **kwargs):
        self.sent.append((QueueUrl, MessageBody))
        return {"MessageId": "sqs-1"}

    def get_queue_attributes(self, QueueUrl=None, AttributeNames=None, **kwargs):
        return {"Attributes": {"QueueArn": "arn:aws:sqs:us-east-1:000000000000:queue"}}


def test_sqs_queue_against_fake_client():
    client = _FakeSqsClient()
    queue = storage.SqsFulfillmentQueue(queue_name="order-fulfillment-queue", client=client)

    message_id = queue.send_fulfillment({"order_id": "o-1", "total_amount": Decimal("9.99")})
    assert message_id == "sqs-1"
    url, body = client.sent[0]
    assert url.endswith("order-fulfillment-queue")
    assert json.loads(body)["total_amount"] == 9.99
    assert queue.health()["queue_url"].endswith("order-fulfillment-queue")


class _FakeSnsClient:
    def __init__(self, arn="arn:aws:sns:us-east-1:000000000000:order-status-changed-topic"):
        self.arn = arn
        self.published = []

    def list_topics(self, **kwargs):
        return {"Topics": [{"TopicArn": self.arn}]}

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "sns-1"}

    def get_topic_attributes(self, TopicArn=None, **kwargs):
        return {"Attributes": {"TopicArn": TopicArn}}


def test_sns_notifier_against_fake_client():
    client = _FakeSnsClient()
    notifier = storage.SnsOrderNotifier(topic_name="order-status-changed-topic", client=client)

    message_id = notifier.publish_status_changed(
        {"order_id": "o-1", "new_status": "FULFILLED", "event_type": "order.status_changed"}
    )
    assert message_id == "sns-1"
    published = client.published[0]
    assert published["TopicArn"] == client.arn
    assert json.loads(published["Message"])["new_status"] == "FULFILLED"
    assert published["MessageAttributes"]["order_id"]["StringValue"] == "o-1"
    assert notifier.health()["topic_arn"] == client.arn


def test_sns_notifier_raises_when_topic_missing():
    notifier = storage.SnsOrderNotifier(topic_name="other-topic", client=_FakeSnsClient())
    with pytest.raises(storage.StorageError):
        notifier.topic_arn()


def test_default_dependency_factories_are_cached(monkeypatch):
    monkeypatch.setattr(app_module, "_REPOSITORY", None)
    monkeypatch.setattr(app_module, "_QUEUE", None)
    monkeypatch.setattr(app_module, "_NOTIFIER", None)

    repo = app_module.get_repository()
    queue = app_module.get_queue()
    notifier = app_module.get_notifier()

    assert isinstance(repo, storage.DynamoOrderRepository)
    assert isinstance(queue, storage.SqsFulfillmentQueue)
    assert isinstance(notifier, storage.SnsOrderNotifier)
    assert app_module.get_repository() is repo
    assert app_module.get_queue() is queue
    assert app_module.get_notifier() is notifier
