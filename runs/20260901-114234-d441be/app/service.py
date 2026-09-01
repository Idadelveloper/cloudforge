"""Point-accrual logic and the SQS consumer.

The functions here take a repository object (see ``storage.LoyaltyRepository``)
so they can be exercised with an in-memory fake in tests.
"""
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, List

import storage

LOGGER = logging.getLogger(__name__)

STANDARD_TIER = "standard"
GOLD_TIER = "gold"
CENTS_PER_POINT = 100
DEFAULT_GOLD_THRESHOLD = 1000


def gold_threshold() -> int:
    try:
        return int(os.environ.get("LOYALTY_GOLD_THRESHOLD", str(DEFAULT_GOLD_THRESHOLD)))
    except (TypeError, ValueError):
        return DEFAULT_GOLD_THRESHOLD


def points_for_amount(amount_cents: Any) -> int:
    """One point per whole currency unit, rounded down."""
    try:
        cents = int(amount_cents)
    except (TypeError, ValueError):
        return 0
    if cents <= 0:
        return 0
    return cents // CENTS_PER_POINT


def new_transaction_id() -> str:
    return "{0:013d}-{1}".format(int(time.time() * 1000), uuid.uuid4().hex[:12])


def process_purchase_message(repo: Any, message: Dict[str, Any]) -> Dict[str, Any]:
    """Award points for one queued purchase message, exactly once."""
    message = message or {}
    key = message.get("idempotency_key")
    customer_id = message.get("customer_id")
    if not key or not customer_id:
        LOGGER.warning("discarding malformed purchase message: %s", message)
        return {"status": "skipped", "reason": "malformed_message"}

    record = repo.get_idempotency(key)
    if record is None:
        return {"status": "skipped", "reason": "unknown_idempotency_key", "idempotency_key": key}
    if record.get("status") == "processed":
        return {
            "status": "skipped",
            "reason": "already_processed",
            "idempotency_key": key,
            "customer_id": customer_id,
            "transaction_id": record.get("transaction_id"),
            "points_awarded": record.get("points_awarded"),
        }
    if not repo.begin_processing(key):
        return {
            "status": "skipped",
            "reason": "not_pending",
            "idempotency_key": key,
            "customer_id": customer_id,
        }

    try:
        return _award_points(repo, key, customer_id, message)
    except storage.CustomerNotFound:
        repo.finish_idempotency(key, "failed")
        LOGGER.warning("purchase %s references unknown customer %s", key, customer_id)
        return {
            "status": "failed",
            "reason": "unknown_customer",
            "idempotency_key": key,
            "customer_id": customer_id,
        }
    except Exception as exc:  # noqa: BLE001 - worker must not crash the poller
        LOGGER.exception("failed to process purchase %s", key)
        repo.finish_idempotency(key, "failed")
        return {
            "status": "error",
            "reason": str(exc),
            "idempotency_key": key,
            "customer_id": customer_id,
        }


def _award_points(repo: Any, key: str, customer_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
    customer = repo.get_customer(customer_id)
    if customer is None:
        raise storage.CustomerNotFound(customer_id)

    points = points_for_amount(message.get("amount_cents", 0))
    result = repo.increment_points(customer_id, points)
    balance_before = int(result["balance_before"])
    balance_after = int(result["balance_after"])
    tier_before = result.get("tier_before") or STANDARD_TIER
    tier_after = tier_before
    upgraded = False

    if balance_after >= gold_threshold() and tier_before == STANDARD_TIER:
        upgraded = bool(repo.upgrade_tier(customer_id))
        tier_after = GOLD_TIER

    now = storage.utc_now_iso()
    transaction_id = new_transaction_id()
    transaction = {
        "customer_id": customer_id,
        "transaction_id": transaction_id,
        "idempotency_key": key,
        "amount_cents": int(message.get("amount_cents", 0)),
        "currency": message.get("currency", "USD"),
        "order_id": message.get("order_id"),
        "points_awarded": points,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "tier_after": tier_after,
        "created_at": now,
    }
    repo.put_transaction(transaction)

    repo.put_audit_entry(
        {
            "event_id": uuid.uuid4().hex,
            "event_type": "points_accrued",
            "customer_id": customer_id,
            "transaction_id": transaction_id,
            "idempotency_key": key,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "points_delta": points,
            "tier_before": tier_before,
            "tier_after": tier_after,
            "recorded_at": now,
        }
    )

    if upgraded:
        repo.put_audit_entry(
            {
                "event_id": uuid.uuid4().hex,
                "event_type": "tier_upgraded",
                "customer_id": customer_id,
                "transaction_id": transaction_id,
                "idempotency_key": key,
                "balance_before": balance_before,
                "balance_after": balance_after,
                "points_delta": 0,
                "tier_before": tier_before,
                "tier_after": GOLD_TIER,
                "recorded_at": now,
            }
        )
        repo.publish_gold_upgrade(
            {
                "customer_id": customer_id,
                "email": customer.get("email"),
                "previous_tier": tier_before,
                "new_tier": GOLD_TIER,
                "points_balance": balance_after,
                "upgraded_at": now,
            }
        )

    repo.finish_idempotency(key, "processed", transaction_id=transaction_id, points_awarded=points)
    return {
        "status": "processed",
        "idempotency_key": key,
        "customer_id": customer_id,
        "transaction_id": transaction_id,
        "points_awarded": points,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "tier_after": tier_after,
        "gold_upgrade": upgraded,
    }


def drain_queue(repo: Any, max_messages: int = 10) -> List[Dict[str, Any]]:
    """Receive and process up to ``max_messages`` queued purchases."""
    results: List[Dict[str, Any]] = []
    remaining = max(0, int(max_messages))
    while remaining > 0:
        batch = repo.receive_purchases(min(10, remaining))
        if not batch:
            break
        for receipt_handle, body in batch:
            result = process_purchase_message(repo, body)
            results.append(result)
            remaining -= 1
            if result.get("status") != "error":
                repo.delete_message(receipt_handle)
    return results


class BackgroundPoller:
    """Simple daemon thread that keeps draining the purchase queue."""

    def __init__(
        self,
        repo_factory: Callable[[], Any],
        interval_seconds: float = 5.0,
        batch_size: int = 10,
    ) -> None:
        self._repo_factory = repo_factory
        self._interval = float(interval_seconds)
        self._batch_size = int(batch_size)
        self._stop_event = threading.Event()
        self._thread: Any = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="loyalty-sqs-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)

    def _run(self) -> None:  # pragma: no cover - exercised only when enabled
        while not self._stop_event.is_set():
            try:
                drain_queue(self._repo_factory(), max_messages=self._batch_size)
            except Exception as exc:  # noqa: BLE001 - keep the poller alive
                LOGGER.warning("poller iteration failed: %s", exc)
            self._stop_event.wait(self._interval)
