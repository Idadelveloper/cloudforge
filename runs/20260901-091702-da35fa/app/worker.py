"""SQS triggered fulfilment worker.

Deployed as the ``order-fulfillment-worker`` Lambda. For each fulfilment
message it advances the order status in DynamoDB and publishes an
``OrderStatusChangedEvent`` to SNS.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from storage import (
    DynamoOrderRepository,
    NotFoundError,
    OrderNotifier,
    OrderRepository,
    SnsOrderNotifier,
    StorageError,
    utc_now_iso,
)

LOGGER = logging.getLogger("order_fulfillment_worker")
LOGGER.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))


def target_status() -> str:
    """Status the worker moves successfully processed orders to."""
    return os.environ.get("FULFILLMENT_TARGET_STATUS", "FULFILLED")


def process_record(
    record: Dict[str, Any],
    repository: OrderRepository,
    notifier: OrderNotifier,
) -> Dict[str, Any]:
    """Process a single SQS record and return the published event."""
    body = record.get("body") or "{}"
    try:
        message = json.loads(body)
    except ValueError as exc:
        raise ValueError("invalid message body: %s" % exc) from exc
    if not isinstance(message, dict):
        raise ValueError("message body must be a JSON object")

    order_id = message.get("order_id")
    if not order_id:
        raise ValueError("message is missing order_id")

    order = repository.get_order(order_id)
    if order is None:
        raise NotFoundError("order '%s' not found" % order_id)

    previous_status = order.get("status")
    new_status = target_status()
    updated = repository.update_status(order_id, new_status, reason="processed by fulfilment worker")

    event = {
        "order_id": order_id,
        "customer_id": updated.get("customer_id", order.get("customer_id")),
        "previous_status": previous_status,
        "new_status": new_status,
        "reason": "processed by fulfilment worker",
        "changed_at": updated.get("updated_at") or utc_now_iso(),
        "event_type": "order.status_changed",
    }
    notifier.publish_status_changed(event)
    return event


def handler(
    event: Optional[Dict[str, Any]],
    context: Any = None,
    repository: Optional[OrderRepository] = None,
    notifier: Optional[OrderNotifier] = None,
) -> Dict[str, Any]:
    """Lambda entrypoint for the SQS event source mapping."""
    repo = repository or DynamoOrderRepository()
    publisher = notifier or SnsOrderNotifier()

    processed = 0
    failures: List[Dict[str, str]] = []
    records = (event or {}).get("Records") or []
    for record in records:
        try:
            process_record(record, repo, publisher)
            processed += 1
        except (ValueError, StorageError) as exc:
            LOGGER.error("failed to process record %s: %s", record.get("messageId", "?"), exc)
            failures.append({"itemIdentifier": str(record.get("messageId", ""))})

    return {"processed": processed, "batchItemFailures": failures}
