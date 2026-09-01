"""FastAPI application exposing the order processing REST API.

The API accepts new orders, persists them in DynamoDB, enqueues a fulfilment
message on SQS and publishes order-status-changed events to SNS.
All AWS access is hidden behind the small interfaces in ``storage.py`` so the
application can be exercised offline with in-memory implementations.
"""

import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query

import storage
from models import CreateOrderRequest, OrderItem, OrderStatusUpdateRequest
from storage import (
    ALLOWED_STATUSES,
    DynamoOrderRepository,
    InvalidTokenError,
    NotFoundError,
    SnsOrderNotifier,
    SqsFulfillmentQueue,
    StorageError,
    new_order_id,
    quantize_amount,
    utc_now_iso,
)

APP_NAME = "order_processing_service"
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100

logging.basicConfig(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
LOGGER = logging.getLogger("order_processing_service")

app = FastAPI(
    title="Order Processing Service",
    version="1.0.0",
    description="Create orders, track their status and list orders by customer.",
)

_REPOSITORY: Optional[storage.OrderRepository] = None
_QUEUE: Optional[storage.FulfillmentQueue] = None
_NOTIFIER: Optional[storage.OrderNotifier] = None


def get_repository() -> storage.OrderRepository:
    """Return the (lazily created) order repository."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = DynamoOrderRepository()
    return _REPOSITORY


def get_queue() -> storage.FulfillmentQueue:
    """Return the (lazily created) fulfilment queue publisher."""
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = SqsFulfillmentQueue()
    return _QUEUE


def get_notifier() -> storage.OrderNotifier:
    """Return the (lazily created) SNS notifier."""
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = SnsOrderNotifier()
    return _NOTIFIER


def _dump(model: Any) -> Dict[str, Any]:
    """Dump a pydantic model to a plain dict (pydantic v1 and v2 compatible)."""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _item_payload(item: OrderItem) -> Dict[str, Any]:
    data = _dump(item)
    return {
        "sku": data["sku"],
        "name": data.get("name"),
        "quantity": int(data["quantity"]),
        "unit_price": quantize_amount(data["unit_price"]),
    }


def _items_payload(items: List[OrderItem]) -> List[Dict[str, Any]]:
    return [_item_payload(item) for item in items]


def _order_total(items: List[OrderItem]) -> Decimal:
    total = Decimal("0")
    for item in items:
        data = _dump(item)
        total += Decimal(str(data["unit_price"])) * Decimal(int(data["quantity"]))
    return quantize_amount(total)


def serialize_order(order: Dict[str, Any]) -> Dict[str, Any]:
    """Convert DynamoDB/Decimal values into JSON friendly primitives."""
    return storage.from_dynamo(dict(order))


def _normalise_status(value: str) -> str:
    normalised = (value or "").strip().upper()
    if normalised not in ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="invalid status '%s'; allowed values: %s" % (value, ", ".join(ALLOWED_STATUSES)),
        )
    return normalised


@app.get("/health")
def health(
    repository: storage.OrderRepository = Depends(get_repository),
    queue: storage.FulfillmentQueue = Depends(get_queue),
    notifier: storage.OrderNotifier = Depends(get_notifier),
) -> Dict[str, Any]:
    """Report service liveness plus reachability of DynamoDB, SQS and SNS."""
    dependencies: Dict[str, Any] = {}
    overall = "ok"
    for name, dependency in (("dynamodb", repository), ("sqs", queue), ("sns", notifier)):
        try:
            dependency.health()
            dependencies[name] = "ok"
        except Exception as exc:  # noqa: B902 - health must never raise
            LOGGER.warning("health check for %s failed: %s", name, exc)
            dependencies[name] = ("error: %s" % exc)[:200]
            overall = "degraded"
    return {"status": overall, "service": APP_NAME, "dependencies": dependencies}


@app.post("/orders", status_code=201)
def create_order(
    payload: CreateOrderRequest,
    repository: storage.OrderRepository = Depends(get_repository),
    queue: storage.FulfillmentQueue = Depends(get_queue),
) -> Dict[str, Any]:
    """Persist a new order then publish a fulfilment message to SQS."""
    if not payload.items:
        raise HTTPException(status_code=422, detail="items must contain at least one entry")

    now = utc_now_iso()
    total = _order_total(payload.items)
    order: Dict[str, Any] = {
        "order_id": new_order_id(),
        "customer_id": payload.customer_id,
        "items": _items_payload(payload.items),
        "total_amount": total,
        "currency": (payload.currency or "USD").upper(),
        "status": "PENDING",
        "created_at": now,
        "updated_at": now,
        "shipping_address": payload.shipping_address,
        "notes": payload.notes,
    }

    try:
        repository.put_order(order)
    except StorageError as exc:
        LOGGER.error("failed to persist order: %s", exc)
        raise HTTPException(status_code=502, detail="failed to persist order: %s" % exc)

    message = {
        "order_id": order["order_id"],
        "customer_id": order["customer_id"],
        "total_amount": total,
        "status": "PENDING",
        "event_type": "order.created",
        "occurred_at": now,
    }
    try:
        queue.send_fulfillment(message)
    except StorageError as exc:
        LOGGER.error("failed to enqueue fulfilment message: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="order %s stored with status PENDING but could not be queued: %s" % (order["order_id"], exc),
        )

    try:
        updated = repository.update_status(
            order["order_id"], "QUEUED", reason="fulfilment message enqueued"
        )
        if updated:
            order = updated
    except StorageError as exc:
        LOGGER.warning("could not mark order %s as QUEUED: %s", order["order_id"], exc)
        order["status"] = "QUEUED"

    return serialize_order(order)


@app.get("/orders")
def list_orders(
    customer_id: str = Query(..., min_length=1, description="Customer whose orders should be listed"),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    next_token: Optional[str] = Query(None),
    repository: storage.OrderRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List orders for a customer using the DynamoDB customer GSI."""
    return _list_for_customer(repository, customer_id, status_filter, limit, next_token)


@app.get("/customers/{customer_id}/orders")
def list_customer_orders(
    customer_id: str,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    next_token: Optional[str] = Query(None),
    repository: storage.OrderRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Path based alias of ``GET /orders?customer_id=...``."""
    return _list_for_customer(repository, customer_id, status_filter, limit, next_token)


def _list_for_customer(
    repository: storage.OrderRepository,
    customer_id: str,
    status_filter: Optional[str],
    limit: int,
    next_token: Optional[str],
) -> Dict[str, Any]:
    normalised_status = _normalise_status(status_filter) if status_filter else None
    try:
        orders, token = repository.list_by_customer(
            customer_id, limit=limit, next_token=next_token, status=normalised_status
        )
    except InvalidTokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except StorageError as exc:
        LOGGER.error("failed to list orders: %s", exc)
        raise HTTPException(status_code=502, detail="failed to list orders: %s" % exc)

    serialized = [serialize_order(order) for order in orders]
    return {
        "customer_id": customer_id,
        "orders": serialized,
        "count": len(serialized),
        "next_token": token,
    }


@app.get("/orders/{order_id}")
def get_order(
    order_id: str,
    repository: storage.OrderRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Return the full order record."""
    order = _load_order(repository, order_id)
    return serialize_order(order)


@app.get("/orders/{order_id}/status")
def get_order_status(
    order_id: str,
    repository: storage.OrderRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Return only the order status and its last update timestamp."""
    order = _load_order(repository, order_id)
    return {
        "order_id": order.get("order_id", order_id),
        "status": order.get("status"),
        "updated_at": order.get("updated_at"),
    }


@app.patch("/orders/{order_id}/status")
def update_order_status(
    order_id: str,
    payload: OrderStatusUpdateRequest,
    repository: storage.OrderRepository = Depends(get_repository),
    notifier: storage.OrderNotifier = Depends(get_notifier),
) -> Dict[str, Any]:
    """Update an order status and publish an SNS notification."""
    new_status = _normalise_status(payload.status)
    existing = _load_order(repository, order_id)
    previous_status = existing.get("status")

    try:
        updated = repository.update_status(order_id, new_status, reason=payload.reason)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="order '%s' not found" % order_id)
    except StorageError as exc:
        LOGGER.error("failed to update order %s: %s", order_id, exc)
        raise HTTPException(status_code=502, detail="failed to update order: %s" % exc)

    changed_at = updated.get("updated_at") or utc_now_iso()
    event = {
        "order_id": order_id,
        "customer_id": updated.get("customer_id", existing.get("customer_id")),
        "previous_status": previous_status,
        "new_status": new_status,
        "reason": payload.reason,
        "changed_at": changed_at,
        "event_type": "order.status_changed",
    }

    notified = True
    try:
        notifier.publish_status_changed(event)
    except StorageError as exc:
        notified = False
        LOGGER.warning("could not publish status change for %s: %s", order_id, exc)

    return {
        "order_id": order_id,
        "status": new_status,
        "previous_status": previous_status,
        "updated_at": changed_at,
        "notified": notified,
    }


def _load_order(repository: storage.OrderRepository, order_id: str) -> Dict[str, Any]:
    try:
        order = repository.get_order(order_id)
    except StorageError as exc:
        LOGGER.error("failed to read order %s: %s", order_id, exc)
        raise HTTPException(status_code=502, detail="failed to read order: %s" % exc)
    if not order:
        raise HTTPException(status_code=404, detail="order '%s' not found" % order_id)
    return order


def main() -> None:
    """Run the service with uvicorn (development / harness entrypoint)."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
