"""Offline tests for the loyalty points service.

Every AWS interaction is replaced by an in-memory fake repository, so the suite
runs without LocalStack, credentials or any network access.
"""

import copy
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as app_module  # noqa: E402
import storage  # noqa: E402
import worker  # noqa: E402


class FakeRepository(object):
    """In-memory stand-in for :class:`storage.LoyaltyRepository`."""

    def __init__(self):
        self.customers = {}
        self.transactions = {}
        self.idempotency = {}
        self.queue = []
        self.audit = {}
        self.published = []
        self._receipts = 0

    # customers -------------------------------------------------------
    def create_customer(self, customer_id, email, name):
        if customer_id in self.customers:
            return None
        now = storage.utcnow_iso()
        item = {
            "customer_id": customer_id,
            "email": email,
            "name": name,
            "points_balance": 0,
            "tier": "standard",
            "created_at": now,
            "updated_at": now,
        }
        self.customers[customer_id] = item
        return copy.deepcopy(item)

    def get_customer(self, customer_id):
        item = self.customers.get(customer_id)
        return copy.deepcopy(item) if item else None

    def increment_balance(self, customer_id, points):
        item = self.customers.get(customer_id)
        if item is None:
            return None
        item["points_balance"] = int(item.get("points_balance", 0)) + int(points)
        item["updated_at"] = storage.utcnow_iso()
        return copy.deepcopy(item)

    def upgrade_tier(self, customer_id, new_tier="gold"):
        item = self.customers.get(customer_id)
        if item is None or item.get("tier") == new_tier:
            return False
        item["tier"] = new_tier
        item["updated_at"] = storage.utcnow_iso()
        return True

    # transactions ----------------------------------------------------
    def put_transaction(self, item):
        self.transactions.setdefault(item["customer_id"], []).append(copy.deepcopy(item))
        return item

    def update_transaction(self, customer_id, transaction_id, status, points_awarded=None, balance_after=None):
        for txn in self.transactions.get(customer_id, []):
            if txn["transaction_id"] == transaction_id:
                txn["status"] = status
                if points_awarded is not None:
                    txn["points_awarded"] = int(points_awarded)
                if balance_after is not None:
                    txn["balance_after"] = int(balance_after)

    def list_transactions(self, customer_id, limit=50, cursor=None):
        items = sorted(
            self.transactions.get(customer_id, []),
            key=lambda txn: txn["transaction_id"],
            reverse=True,
        )
        decoded = storage.decode_cursor(cursor)
        start = int(decoded.get("offset", 0)) if decoded else 0
        page = items[start:start + limit]
        next_cursor = None
        if start + limit < len(items):
            next_cursor = storage.encode_cursor({"offset": start + limit})
        return copy.deepcopy(page), next_cursor

    # idempotency -----------------------------------------------------
    def reserve_idempotency_record(self, key, customer_id, transaction_id, payload):
        if key in self.idempotency:
            return None
        item = {
            "idempotency_key": key,
            "customer_id": customer_id,
            "transaction_id": transaction_id,
            "status": "reserved",
            "response_payload": copy.deepcopy(payload),
            "created_at": storage.utcnow_iso(),
            "expires_at": 0,
        }
        self.idempotency[key] = item
        return copy.deepcopy(item)

    def get_idempotency_record(self, key):
        item = self.idempotency.get(key)
        return copy.deepcopy(item) if item else None

    def claim_idempotency_record(self, key):
        item = self.idempotency.get(key)
        if item is None or item.get("status") != "reserved":
            return False
        item["status"] = "processing"
        return True

    def complete_idempotency_record(self, key, status, payload):
        item = self.idempotency.get(key)
        if item is None:
            return
        item["status"] = status
        item["response_payload"] = copy.deepcopy(payload)

    # queue -----------------------------------------------------------
    def enqueue_purchase(self, message):
        self._receipts += 1
        handle = "receipt-{0}".format(self._receipts)
        self.queue.append({"receipt_handle": handle, "body": copy.deepcopy(message)})
        return handle

    def receive_purchase_messages(self, max_messages=10, wait_seconds=0):
        return copy.deepcopy(self.queue[:max_messages])

    def delete_purchase_message(self, receipt_handle):
        self.queue = [msg for msg in self.queue if msg["receipt_handle"] != receipt_handle]

    # audit log -------------------------------------------------------
    def put_audit_entry(self, entry):
        record = dict(entry)
        recorded_at = record.get("recorded_at") or storage.utcnow_iso()
        record["recorded_at"] = recorded_at
        record.setdefault("audit_id", "audit-{0}".format(len(self.audit) + 1))
        key = storage.audit_key(
            record.get("customer_id", "unknown"),
            recorded_at,
            "{0}-{1}".format(record.get("transaction_id", "na"), record.get("event_type", "event")),
        )
        record["s3_key"] = key
        self.audit[key] = record
        return record

    def list_audit_entries(self, customer_id, limit=50):
        prefix = "customers/{0}/".format(customer_id)
        keys = sorted([key for key in self.audit if key.startswith(prefix)], reverse=True)
        return [{"key": key, "size": len(str(self.audit[key])), "last_modified": None} for key in keys[:limit]]

    def get_audit_entry(self, key):
        entry = self.audit.get(key)
        return copy.deepcopy(entry) if entry else None

    # notifications ---------------------------------------------------
    def publish_tier_upgrade(self, notification):
        self.published.append(copy.deepcopy(notification))
        return "message-{0}".format(len(self.published))

    # health ----------------------------------------------------------
    def health(self):
        return {"dynamodb": "ok", "sqs": "ok", "sns": "ok", "s3": "ok"}


@pytest.fixture()
def repo():
    return FakeRepository()


@pytest.fixture()
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _create_customer(client, customer_id="cust-1"):
    response = client.post(
        "/customers",
        json={"customer_id": customer_id, "email": "a@example.com", "name": "Ada"},
    )
    assert response.status_code == 201
    return response.json()


def _submit_purchase(client, customer_id, key, amount_cents, order_id="order-1"):
    return client.post(
        "/purchases",
        json={"customer_id": customer_id, "order_id": order_id, "amount_cents": amount_cents},
        headers={"Idempotency-Key": key},
    )


def test_root_and_health(client):
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["service"] == "loyalty_points_service"

    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["dependencies"]["dynamodb"] == "ok"


def test_create_customer_and_conflict(client):
    created = _create_customer(client)
    assert created["points_balance"] == 0
    assert created["tier"] == "standard"

    duplicate = client.post(
        "/customers",
        json={"customer_id": "cust-1", "email": "a@example.com", "name": "Ada"},
    )
    assert duplicate.status_code == 409


def test_create_customer_generates_id(client):
    response = client.post("/customers", json={"email": "b@example.com", "name": "Grace"})
    assert response.status_code == 201
    assert response.json()["customer_id"]


def test_get_customer_and_balance(client):
    _create_customer(client)
    profile = client.get("/customers/cust-1")
    assert profile.status_code == 200
    assert profile.json()["email"] == "a@example.com"

    balance = client.get("/customers/cust-1/balance")
    assert balance.status_code == 200
    assert balance.json() == {
        "customer_id": "cust-1",
        "points_balance": 0,
        "tier": "standard",
        "updated_at": profile.json()["updated_at"],
    }

    assert client.get("/customers/nope").status_code == 404
    assert client.get("/customers/nope/balance").status_code == 404


def test_submit_purchase_enqueues_once_and_is_idempotent(client, repo):
    _create_customer(client)
    first = _submit_purchase(client, "cust-1", "key-1", 2550)
    assert first.status_code == 202
    payload = first.json()
    assert payload["points"] == 25
    assert payload["duplicate"] is False
    assert len(repo.queue) == 1

    second = _submit_purchase(client, "cust-1", "key-1", 2550)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["transaction_id"] == payload["transaction_id"]
    assert len(repo.queue) == 1


def test_submit_purchase_requires_idempotency_key(client):
    _create_customer(client)
    response = client.post(
        "/purchases",
        json={"customer_id": "cust-1", "order_id": "o1", "amount_cents": 100},
    )
    assert response.status_code == 400


def test_submit_purchase_accepts_body_key(client, repo):
    _create_customer(client)
    response = client.post(
        "/purchases",
        json={
            "customer_id": "cust-1",
            "order_id": "o1",
            "amount_cents": 100,
            "idempotency_key": "body-key",
        },
    )
    assert response.status_code == 202
    assert "body-key" in repo.idempotency


def test_submit_purchase_unknown_customer_and_validation(client):
    missing = _submit_purchase(client, "ghost", "key-x", 100)
    assert missing.status_code == 404

    _create_customer(client)
    invalid = client.post(
        "/purchases",
        json={"customer_id": "cust-1", "order_id": "o1", "amount_cents": -5},
        headers={"Idempotency-Key": "key-neg"},
    )
    assert invalid.status_code == 422


def test_purchase_status_lookup(client):
    _create_customer(client)
    _submit_purchase(client, "cust-1", "key-1", 1000)
    found = client.get("/purchases/key-1")
    assert found.status_code == 200
    assert found.json()["status"] == "reserved"
    assert client.get("/purchases/unknown-key").status_code == 404


def test_processing_awards_points_and_writes_audit_entry(client, repo):
    _create_customer(client)
    submitted = _submit_purchase(client, "cust-1", "key-1", 5000).json()

    processed = client.post("/internal/process-queue")
    assert processed.status_code == 200
    body = processed.json()
    assert body["processed"] == 1
    assert body["results"][0]["status"] == "applied"
    assert body["results"][0]["points_awarded"] == 50

    balance = client.get("/customers/cust-1/balance").json()
    assert balance["points_balance"] == 50
    assert balance["tier"] == "standard"

    txns = client.get("/customers/cust-1/transactions").json()
    assert txns["count"] == 1
    assert txns["items"][0]["status"] == "applied"
    assert txns["items"][0]["transaction_id"] == submitted["transaction_id"]

    audit = client.get("/customers/cust-1/audit-log", params={"include_entries": "true"}).json()
    assert audit["count"] == 1
    assert audit["entries"][0]["entry"]["event_type"] == "accrual"
    assert audit["entries"][0]["entry"]["balance_after"] == 50

    status = client.get("/purchases/key-1").json()
    assert status["status"] == "completed"
    assert status["result"]["balance_after"] == 50
    assert repo.queue == []


def test_replayed_message_does_not_double_award(client, repo):
    _create_customer(client)
    _submit_purchase(client, "cust-1", "key-1", 5000)
    message = copy.deepcopy(repo.queue[0]["body"])

    first = worker.process_purchase(repo, message)
    assert first["status"] == "applied"

    second = worker.process_purchase(repo, message)
    assert second["duplicate"] is True

    assert repo.customers["cust-1"]["points_balance"] == 50
    assert len(repo.audit) == 1


def test_gold_tier_upgrade_publishes_once(client, repo):
    _create_customer(client)
    _submit_purchase(client, "cust-1", "key-1", 150000, order_id="big-order")
    client.post("/internal/process-queue")

    assert repo.customers["cust-1"]["tier"] == "gold"
    assert len(repo.published) == 1
    notification = repo.published[0]
    assert notification["previous_tier"] == "standard"
    assert notification["new_tier"] == "gold"
    assert notification["balance"] == 1500

    events = [entry["event_type"] for entry in repo.audit.values()]
    assert sorted(events) == ["accrual", "tier_upgrade"]

    _submit_purchase(client, "cust-1", "key-2", 20000, order_id="another")
    client.post("/internal/process-queue")
    assert len(repo.published) == 1
    assert repo.customers["cust-1"]["points_balance"] == 1700


def test_worker_handles_missing_customer(repo):
    repo.reserve_idempotency_record("key-ghost", "ghost", "txn-1", {})
    result = worker.process_purchase(
        repo,
        {
            "idempotency_key": "key-ghost",
            "customer_id": "ghost",
            "transaction_id": "txn-1",
            "points": 10,
        },
    )
    assert result["status"] == "failed"
    assert repo.idempotency["key-ghost"]["status"] == "failed"


def test_worker_rejects_invalid_message(repo):
    assert worker.process_purchase(repo, {})["status"] == "rejected"


def test_lambda_handler_processes_records(monkeypatch, repo):
    monkeypatch.setattr(worker, "get_repository", lambda: repo)
    repo.create_customer("cust-1", "a@example.com", "Ada")
    repo.reserve_idempotency_record("key-l", "cust-1", "txn-l", {})
    repo.put_transaction(
        {"customer_id": "cust-1", "transaction_id": "txn-l", "status": "pending"}
    )
    event = {
        "Records": [
            {
                "messageId": "m1",
                "body": '{"idempotency_key": "key-l", "customer_id": "cust-1", '
                        '"transaction_id": "txn-l", "points": 12}',
            },
            {"messageId": "m2", "body": "not-json"},
        ]
    }
    result = worker.handler(event, None)
    assert result["processed"] == 1
    assert result["batchItemFailures"] == [{"itemIdentifier": "m2"}]
    assert repo.customers["cust-1"]["points_balance"] == 12


def test_transactions_pagination_and_errors(client):
    _create_customer(client)
    _submit_purchase(client, "cust-1", "key-1", 100, order_id="o1")
    _submit_purchase(client, "cust-1", "key-2", 200, order_id="o2")

    page = client.get("/customers/cust-1/transactions", params={"limit": 1}).json()
    assert page["count"] == 1
    assert page["next_cursor"]

    second = client.get(
        "/customers/cust-1/transactions",
        params={"limit": 1, "cursor": page["next_cursor"]},
    ).json()
    assert second["count"] == 1
    assert second["items"][0]["transaction_id"] != page["items"][0]["transaction_id"]
    assert second["next_cursor"] is None

    bad = client.get("/customers/cust-1/transactions", params={"cursor": "!!!"})
    assert bad.status_code == 400
    assert client.get("/customers/ghost/transactions").status_code == 404


def test_audit_log_empty_for_unknown_customer(client):
    response = client.get("/customers/ghost/audit-log")
    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_points_for_amount_rules():
    assert worker.points_for_amount(0) == 0
    assert worker.points_for_amount(99) == 0
    assert worker.points_for_amount(100) == 1
    assert worker.points_for_amount(2599) == 25
    assert worker.points_for_amount("bad") == 0


def test_cursor_round_trip_and_validation():
    cursor = storage.encode_cursor({"customer_id": "c", "transaction_id": "t"})
    assert storage.decode_cursor(cursor) == {"customer_id": "c", "transaction_id": "t"}
    assert storage.decode_cursor(None) is None
    with pytest.raises(ValueError):
        storage.decode_cursor("!!!")


def test_audit_key_layout():
    key = storage.audit_key("cust-1", "2024-05-06T07:08:09.123456Z", "txn-1")
    assert key.startswith("customers/cust-1/2024/05/06/")
    assert key.endswith("-txn-1.json")


def test_clients_use_endpoint_url_and_default_region(monkeypatch):
    captured = {}

    def fake_factory(service_name, **kwargs):
        captured[service_name] = kwargs
        return object()

    monkeypatch.setattr(storage.boto3, "resource", fake_factory)
    monkeypatch.setattr(storage.boto3, "client", fake_factory)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    storage.dynamodb_resource()
    storage.sqs_client()
    storage.sns_client()
    storage.s3_client()

    for service in ("dynamodb", "sqs", "sns", "s3"):
        assert captured[service]["endpoint_url"] == "http://localhost:4566"
        assert captured[service]["region_name"] == "us-east-1"


def test_repository_defaults_from_environment(monkeypatch):
    monkeypatch.delenv("CUSTOMERS_TABLE", raising=False)
    monkeypatch.setenv("AUDIT_BUCKET", "custom-bucket")
    repository = storage.build_repository()
    assert repository.customers_table_name == "loyalty-customers"
    assert repository.transactions_table_name == "loyalty-transactions"
    assert repository.idempotency_table_name == "loyalty-idempotency"
    assert repository.queue_name == "loyalty-purchases-queue"
    assert repository.topic_name == "loyalty-tier-upgrades"
    assert repository.bucket == "custom-bucket"
