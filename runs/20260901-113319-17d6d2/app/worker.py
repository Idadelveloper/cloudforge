"""Asynchronous point-accrual worker.

Consumes purchase messages from SQS, applies idempotent point accrual, writes an
S3 audit entry for every balance change and publishes an SNS notification the
first time a customer's balance crosses the gold-tier threshold.

The module is deployable as a Lambda (``worker.handler``) and is also used by the
API's ``/internal/process-queue`` fallback endpoint.
"""

import json
import os

import storage

GOLD_TIER = "gold"
STANDARD_TIER = "standard"
DEFAULT_GOLD_THRESHOLD = 1000

_REPOSITORY = None


def get_repository():
    """Lazily build the module-level repository (used by the Lambda handler)."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = storage.build_repository()
    return _REPOSITORY


def gold_threshold():
    """Point balance at which a customer becomes gold tier."""
    try:
        return int(os.environ.get("GOLD_TIER_THRESHOLD", str(DEFAULT_GOLD_THRESHOLD)))
    except (TypeError, ValueError):
        return DEFAULT_GOLD_THRESHOLD


def points_for_amount(amount_cents):
    """One point per whole currency unit (floored)."""
    try:
        cents = int(amount_cents)
    except (TypeError, ValueError):
        return 0
    return max(0, cents // 100)


def process_purchase(repo, message):
    """Apply a single purchase message exactly once."""
    key = str(message.get("idempotency_key") or "")
    customer_id = str(message.get("customer_id") or "")
    transaction_id = str(message.get("transaction_id") or "")
    if not key or not customer_id or not transaction_id:
        return {"status": "rejected", "reason": "invalid_message"}

    raw_points = message.get("points")
    if raw_points is None:
        points = points_for_amount(message.get("amount_cents", 0))
    else:
        points = points_for_amount(int(raw_points) * 100)

    record = repo.get_idempotency_record(key) or {}
    if record.get("status") == "completed":
        result = dict(record.get("response_payload") or {})
        result["duplicate"] = True
        result.setdefault("status", "applied")
        return result

    if not repo.claim_idempotency_record(key):
        return {
            "status": "skipped",
            "duplicate": True,
            "idempotency_key": key,
            "customer_id": customer_id,
            "transaction_id": transaction_id,
        }

    customer = repo.increment_balance(customer_id, points)
    if customer is None:
        failure = {
            "status": "failed",
            "reason": "customer_not_found",
            "idempotency_key": key,
            "customer_id": customer_id,
            "transaction_id": transaction_id,
        }
        repo.update_transaction(customer_id, transaction_id, status="failed")
        repo.complete_idempotency_record(key, "failed", failure)
        return failure

    balance_after = int(customer.get("points_balance", 0) or 0)
    balance_before = balance_after - points
    tier_before = str(customer.get("tier") or STANDARD_TIER)
    tier_after = tier_before
    now = storage.utcnow_iso()

    repo.put_audit_entry(
        {
            "customer_id": customer_id,
            "transaction_id": transaction_id,
            "idempotency_key": key,
            "event_type": "accrual",
            "points_delta": points,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "tier_before": tier_before,
            "tier_after": tier_before,
            "recorded_at": now,
        }
    )

    upgraded = False
    if balance_after >= gold_threshold() and tier_before != GOLD_TIER:
        upgraded = bool(repo.upgrade_tier(customer_id, GOLD_TIER))
        if upgraded:
            tier_after = GOLD_TIER
            repo.put_audit_entry(
                {
                    "customer_id": customer_id,
                    "transaction_id": transaction_id,
                    "idempotency_key": key,
                    "event_type": "tier_upgrade",
                    "points_delta": 0,
                    "balance_before": balance_after,
                    "balance_after": balance_after,
                    "tier_before": tier_before,
                    "tier_after": GOLD_TIER,
                    "recorded_at": now,
                }
            )
            repo.publish_tier_upgrade(
                {
                    "customer_id": customer_id,
                    "previous_tier": tier_before,
                    "new_tier": GOLD_TIER,
                    "balance": balance_after,
                    "transaction_id": transaction_id,
                    "occurred_at": now,
                }
            )

    repo.update_transaction(
        customer_id,
        transaction_id,
        status="applied",
        points_awarded=points,
        balance_after=balance_after,
    )

    result = {
        "status": "applied",
        "duplicate": False,
        "idempotency_key": key,
        "customer_id": customer_id,
        "transaction_id": transaction_id,
        "points_awarded": points,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "tier": tier_after,
        "tier_upgraded": upgraded,
    }
    repo.complete_idempotency_record(key, "completed", result)
    return result


def drain_queue(repo, max_messages=10):
    """Receive, process and delete up to ``max_messages`` purchase messages."""
    processed = []
    for message in repo.receive_purchase_messages(max_messages=max_messages):
        result = process_purchase(repo, message.get("body") or {})
        repo.delete_purchase_message(message.get("receipt_handle"))
        processed.append(result)
    return processed


def handler(event, context=None):
    """AWS Lambda entrypoint for SQS-triggered accrual."""
    records = event.get("Records", []) if isinstance(event, dict) else []
    failures = []
    repo = get_repository()
    for record in records:
        try:
            body = json.loads(record.get("body") or "{}")
            process_purchase(repo, body)
        except Exception as exc:  # noqa: BLE001 - report partial batch failure
            failures.append(
                {
                    "itemIdentifier": record.get("messageId"),
                    "error": exc.__class__.__name__,
                }
            )
    return {
        "batchItemFailures": [{"itemIdentifier": item["itemIdentifier"]} for item in failures],
        "processed": len(records) - len(failures),
    }


lambda_handler = handler
