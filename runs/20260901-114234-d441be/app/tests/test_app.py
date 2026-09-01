"""Offline tests: every AWS call is replaced by an in-memory fake repository."""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import config  # noqa: E402
import service  # noqa: E402
import storage  # noqa: E402


class FakeRepository:
    """In-memory stand-in for storage.LoyaltyRepository."""

    def __init__(self):
        self.customers = {}
        self.idempotency = {}
        self.transactions = {}
        self.queue = []
        self.sent = []
        self.deleted = []
        self.audit = []
        self.notifications = []
        self.healthy = True
        self._counter = 0

    # health
    def health(self):
        state = "ok" if self.healthy else "unavailable"
        return {"dynamodb": state, "sqs": state, "sns": state, "s3": state}

    # customers
    def create_customer(self, item):
        if item["customer_id"] in self.customers:
            return False
        self.customers[item["customer_id"]] = dict(item)
        return True

    def get_customer(self, customer_id):
        item = self.customers.get(customer_id)
        return dict(item) if item else None

    def increment_points(self, customer_id, points):
        customer = self.customers.get(customer_id)
        if customer is None:
            raise storage.CustomerNotFound(customer_id)
        before = int(customer.get("points_balance", 0))
        after = before + int(points)
        customer["points_balance"] = after
        customer["lifetime_points"] = int(customer.get("lifetime_points", 0)) + int(points)
        customer["updated_at"] = storage.utc_now_iso()
        return {
            "balance_before": before,
            "balance_after": after,
            "tier_before": customer.get("tier", "standard"),
            "lifetime_points": customer["lifetime_points"],
            "customer": dict(customer),
        }

    def upgrade_tier(self, customer_id):
        customer = self.customers.get(customer_id)
        if customer is None or customer.get("tier") != "standard":
            return False
        customer["tier"] = "gold"
        customer["updated_at"] = storage.utc_now_iso()
        return True

    # idempotency
    def reserve_idempotency(self, record):
        key = record["idempotency_key"]
        if key in self.idempotency:
            return False
        self.idempotency[key] = dict(record)
        return True

    def get_idempotency(self, idempotency_key):
        record = self.idempotency.get(idempotency_key)
        return dict(record) if record else None

    def begin_processing(self, idempotency_key):
        record = self.idempotency.get(idempotency_key)
        if record is None or record.get("status") != "pending":
            return False
        record["status"] = "processing"
        return True

    def finish_idempotency(self, idempotency_key, status, transaction_id=None, points_awarded=None):
        record = self.idempotency.get(idempotency_key)
        if record is None:
            return {}
        record["status"] = status
        record["transaction_id"] = transaction_id
        record["points_awarded"] = points_awarded
        return dict(record)

    # transactions
    def put_transaction(self, transaction):
        self.transactions.setdefault(transaction["customer_id"], []).append(dict(transaction))

    def list_transactions(self, customer_id, limit=50, cursor=None):
        items = sorted(
            self.transactions.get(customer_id, []),
            key=lambda item: item["transaction_id"],
            reverse=True,
        )
        offset = 0
        if cursor:
            offset = int(storage.decode_cursor(cursor).get("offset", 0))
        page = [dict(item) for item in items[offset:offset + limit]]
        next_cursor = None
        if offset + limit < len(items):
            next_cursor = storage.encode_cursor({"offset": offset + limit})
        return page, next_cursor

    # messaging
    def enqueue_purchase(self, message):
        self._counter += 1
        handle = "handle-{0}".format(self._counter)
        self.queue.append((handle, dict(message)))
        self.sent.append(dict(message))
        return "message-{0}".format(self._counter)

    def receive_purchases(self, max_messages=10):
        batch = self.queue[:max_messages]
        self.queue = self.queue[max_messages:]
        return [(handle, dict(body)) for handle, body in batch]

    def delete_message(self, receipt_handle):
        self.deleted.append(receipt_handle)

    def publish_gold_upgrade(self, payload):
        self.notifications.append(dict(payload))
        return "sns-{0}".format(len(self.notifications))

    # audit
    def put_audit_entry(self, entry):
        self.audit.append(dict(entry))
        return "audit/{0}.json".format(entry.get("event_id"))


@pytest.fixture
def repo():
    return FakeRepository()


@pytest.fixture
def client(repo, monkeypatch):
    for name in ("LOYALTY_API_KEY", "LOYALTY_SECRET_NAME", "LOYALTY_ENABLE_POLLER", "LOYALTY_GOLD_THRESHOLD"):
        monkeypatch.delenv(name, raising=False)
    config.reset_cache()
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def make_customer(client, email="alice@example.com", name="Alice", customer_id=None):
    body = {"email": email, "name": name}
    if customer_id:
        body["customer_id"] = customer_id
    response = client.post("/customers", json=body)
    assert response.status_code == 201
    return response.json()["customer_id"]


def submit_purchase(client, customer_id, key, amount_cents, order_id=None):
    payload = {
        "idempotency_key": key,
        "customer_id": customer_id,
        "amount_cents": amount_cents,
    }
    if order_id:
        payload["order_id"] = order_id
    return client.post("/purchases", json=payload)


def test_health_reports_dependencies(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["dependencies"] == {"dynamodb": "ok", "sqs": "ok", "sns": "ok", "s3": "ok"}


def test_health_degraded(client, repo):
    repo.healthy = False
    data = client.get("/health").json()
    assert data["status"] == "degraded"


def test_create_and_fetch_customer(client):
    customer_id = make_customer(client)
    response = client.get("/customers/{0}".format(customer_id))
    assert response.status_code == 200
    body = response.json()
    assert body["points_balance"] == 0
    assert body["tier"] == "standard"
    assert body["email"] == "alice@example.com"


def test_create_duplicate_customer_conflict(client):
    make_customer(client, customer_id="cust-1")
    response = client.post("/customers", json={"email": "b@example.com", "name": "B", "customer_id": "cust-1"})
    assert response.status_code == 409


def test_get_missing_customer_and_balance_404(client):
    assert client.get("/customers/nope").status_code == 404
    assert client.get("/customers/nope/balance").status_code == 404
    assert client.get("/customers/nope/transactions").status_code == 404


def test_balance_endpoint(client):
    customer_id = make_customer(client)
    response = client.get("/customers/{0}/balance".format(customer_id))
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "customer_id": customer_id,
        "points_balance": 0,
        "lifetime_points": 0,
        "tier": "standard",
        "updated_at": body["updated_at"],
    }


def test_purchase_is_queued_and_processed_once(client, repo):
    customer_id = make_customer(client)
    response = submit_purchase(client, customer_id, "key-1", 10000)
    assert response.status_code == 202
    assert response.json()["purchase"]["status"] == "pending"
    assert len(repo.queue) == 1

    drained = client.post("/admin/process-queue")
    assert drained.status_code == 200
    data = drained.json()
    assert data["processed"] == 1
    result = data["results"][0]
    assert result["points_awarded"] == 100
    assert result["balance_after"] == 100
    assert repo.deleted == ["handle-1"]

    balance = client.get("/customers/{0}/balance".format(customer_id)).json()
    assert balance["points_balance"] == 100
    assert balance["tier"] == "standard"
    assert len(repo.audit) == 1
    assert repo.audit[0]["event_type"] == "points_accrued"
    assert repo.notifications == []


def test_duplicate_idempotency_key_is_not_requeued(client, repo):
    customer_id = make_customer(client)
    assert submit_purchase(client, customer_id, "key-dup", 5000).status_code == 202
    second = submit_purchase(client, customer_id, "key-dup", 5000)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(repo.queue) == 1

    client.post("/admin/process-queue")
    assert client.get("/customers/{0}/balance".format(customer_id)).json()["points_balance"] == 50

    # A duplicate submitted after processing still must not award points again.
    third = submit_purchase(client, customer_id, "key-dup", 5000)
    assert third.status_code == 200
    assert third.json()["purchase"]["status"] == "processed"
    client.post("/admin/process-queue")
    assert client.get("/customers/{0}/balance".format(customer_id)).json()["points_balance"] == 50


def test_redelivered_message_does_not_double_award(client, repo):
    customer_id = make_customer(client)
    submit_purchase(client, customer_id, "key-redeliver", 20000)
    client.post("/admin/process-queue")
    assert client.get("/customers/{0}/balance".format(customer_id)).json()["points_balance"] == 200

    # simulate SQS at-least-once redelivery of the very same message body
    repo.enqueue_purchase(repo.sent[0])
    data = client.post("/admin/process-queue").json()
    assert data["processed"] == 0
    assert data["results"][0]["reason"] == "already_processed"
    assert client.get("/customers/{0}/balance".format(customer_id)).json()["points_balance"] == 200
    assert len(repo.transactions[customer_id]) == 1


def test_same_key_different_payload_conflict(client):
    customer_id = make_customer(client)
    submit_purchase(client, customer_id, "key-conflict", 1000)
    response = submit_purchase(client, customer_id, "key-conflict", 9999)
    assert response.status_code == 409


def test_purchase_for_unknown_customer_404(client):
    response = submit_purchase(client, "missing", "key-x", 1000)
    assert response.status_code == 404


def test_purchase_validation_error(client):
    customer_id = make_customer(client)
    response = submit_purchase(client, customer_id, "key-bad", 0)
    assert response.status_code == 422


def test_purchase_status_endpoint(client):
    customer_id = make_customer(client)
    submit_purchase(client, customer_id, "key-status", 30000)
    pending = client.get("/purchases/key-status")
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"

    client.post("/admin/process-queue")
    processed = client.get("/purchases/key-status").json()
    assert processed["status"] == "processed"
    assert processed["points_awarded"] == 300
    assert processed["transaction_id"]

    assert client.get("/purchases/unknown-key").status_code == 404


def test_gold_tier_upgrade_notifies_once(client, repo):
    customer_id = make_customer(client, email="gold@example.com")
    submit_purchase(client, customer_id, "gold-1", 150000)
    result = client.post("/admin/process-queue").json()["results"][0]
    assert result["gold_upgrade"] is True
    assert result["tier_after"] == "gold"

    balance = client.get("/customers/{0}/balance".format(customer_id)).json()
    assert balance["points_balance"] == 1500
    assert balance["tier"] == "gold"

    assert len(repo.notifications) == 1
    notification = repo.notifications[0]
    assert notification["new_tier"] == "gold"
    assert notification["previous_tier"] == "standard"
    assert notification["email"] == "gold@example.com"
    event_types = [entry["event_type"] for entry in repo.audit]
    assert event_types == ["points_accrued", "tier_upgraded"]

    # further purchases must not re-notify
    submit_purchase(client, customer_id, "gold-2", 100000)
    second = client.post("/admin/process-queue").json()["results"][0]
    assert second["gold_upgrade"] is False
    assert len(repo.notifications) == 1
    assert len(repo.audit) == 3


def test_transactions_pagination(client, repo):
    customer_id = make_customer(client)
    for index in range(3):
        submit_purchase(client, customer_id, "tx-{0}".format(index), 10000 + index * 100)
    client.post("/admin/process-queue")

    first = client.get("/customers/{0}/transactions".format(customer_id), params={"limit": 2}).json()
    assert first["count"] == 2
    assert first["next_cursor"]
    second = client.get(
        "/customers/{0}/transactions".format(customer_id),
        params={"limit": 2, "cursor": first["next_cursor"]},
    ).json()
    assert second["count"] == 1
    assert second["next_cursor"] is None

    ids = {item["transaction_id"] for item in first["transactions"] + second["transactions"]}
    assert len(ids) == 3
    assert len(repo.transactions[customer_id]) == 3


def test_transactions_invalid_cursor(client):
    customer_id = make_customer(client)
    response = client.get(
        "/customers/{0}/transactions".format(customer_id),
        params={"cursor": "!!!not-base64!!!"},
    )
    assert response.status_code == 400


def test_process_queue_when_empty(client):
    data = client.post("/admin/process-queue").json()
    assert data == {"received": 0, "processed": 0, "skipped": 0, "results": []}


def test_api_key_enforced_when_configured(client, monkeypatch):
    monkeypatch.setenv("LOYALTY_API_KEY", "unit-test-shared-value")
    config.reset_cache()
    unauthorised = client.post("/customers", json={"email": "c@example.com", "name": "C"})
    assert unauthorised.status_code == 401
    authorised = client.post(
        "/customers",
        json={"email": "c@example.com", "name": "C"},
        headers={"X-API-Key": "unit-test-shared-value"},
    )
    assert authorised.status_code == 201
    assert client.get("/health").status_code == 200


def test_worker_handles_unknown_customer(repo):
    repo.reserve_idempotency(
        {
            "idempotency_key": "orphan",
            "customer_id": "ghost",
            "status": "pending",
            "request_fingerprint": "abc",
        }
    )
    result = service.process_purchase_message(
        repo, {"idempotency_key": "orphan", "customer_id": "ghost", "amount_cents": 500}
    )
    assert result["status"] == "failed"
    assert result["reason"] == "unknown_customer"
    assert repo.get_idempotency("orphan")["status"] == "failed"


def test_worker_skips_malformed_and_unknown_messages(repo):
    assert service.process_purchase_message(repo, {})["reason"] == "malformed_message"
    message = {"idempotency_key": "never-seen", "customer_id": "c", "amount_cents": 100}
    assert service.process_purchase_message(repo, message)["reason"] == "unknown_idempotency_key"


def test_points_for_amount_rounds_down():
    assert service.points_for_amount(99) == 0
    assert service.points_for_amount(100) == 1
    assert service.points_for_amount(1999) == 19
    assert service.points_for_amount(-5) == 0
    assert service.points_for_amount("bad") == 0


def test_cursor_roundtrip():
    cursor = storage.encode_cursor({"customer_id": "c1", "transaction_id": "t1"})
    assert storage.decode_cursor(cursor) == {"customer_id": "c1", "transaction_id": "t1"}
    with pytest.raises(ValueError):
        storage.decode_cursor("@@@@")


def test_repository_reads_endpoint_from_environment(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("LOYALTY_AUDIT_BUCKET", "custom-bucket")
    repository = storage.LoyaltyRepository()
    assert storage.endpoint_url() == "http://localhost:4566"
    assert storage.region_name() == "us-east-1"
    assert repository.audit_bucket == "custom-bucket"
    assert repository.customers_table_name == "loyalty-customers"
