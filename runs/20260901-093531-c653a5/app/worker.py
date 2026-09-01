"""Asynchronous fulfilment worker.

Deployed as the Lambda consumer of the order fulfilment SQS queue. It advances
the order status in DynamoDB and publishes the change to the SNS topic.
"""
import json
import logging
from typing import Any, Dict, Optional

from storage import (
    AwsEventPublisher,
    DynamoOrderRepository,
    EventPublisher,
    OrderRepository,
    StorageError,
    utc_now,
)

LOGGER = logging.getLogger("order_processing_service.worker")

FULFILLED = "FULFILLED"


def handler(
    event: Dict[str, Any],
    context: Any = None,
    repo: Optional[OrderRepository] = None,
    publisher: Optional[EventPublisher] = None,
) -> Dict[str, Any]:
    """Process a batch of SQS records, returning partial-batch failures."""
    repository = repo or DynamoOrderRepository()
    events = publisher or AwsEventPublisher()
    processed = []
    failures = []

    for record in event.get("Records", []) or []:
        message_id = record.get("messageId", "unknown")
        try:
            body = json.loads(record.get("body") or "{}")
        except (TypeError, ValueError):
            LOGGER.error("record %s has a malformed body", message_id)
            failures.append(message_id)
            continue

        order_id = body.get("order_id")
        if not order_id:
            LOGGER.error("record %s has no order_id", message_id)
            failures.append(message_id)
            continue

        try:
            existing = repository.get_order(order_id)
            if existing is None:
                LOGGER.error("order %s not found while fulfilling", order_id)
                failures.append(message_id)
                continue
            updated = repository.update_status(order_id, FULFILLED, reason="fulfilment worker")
            if updated is None:
                failures.append(message_id)
                continue
            events.publish_status_event(
                {
                    "order_id": order_id,
                    "customer_id": updated.get("customer_id", ""),
                    "previous_status": existing.get("status"),
                    "new_status": FULFILLED,
                    "reason": "fulfilment worker",
                    "changed_at": updated.get("updated_at", utc_now()),
                }
            )
            processed.append(order_id)
        except StorageError as exc:
            LOGGER.error("failed to process record %s: %s", message_id, exc)
            failures.append(message_id)

    return {
        "processed": processed,
        "batchItemFailures": [{"itemIdentifier": item} for item in failures],
    }
