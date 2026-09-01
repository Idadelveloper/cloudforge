"""FastAPI application for the order-processing service.

Endpoints:
    POST   /orders                  create an order (DynamoDB + SQS + SNS)
    GET    /orders/{order_id}       fetch a single order
    GET    /orders                  list orders for a customer (GSI query)
    PATCH  /orders/{order_id}/status update status and notify subscribers
    GET    /health                  liveness / configuration probe
"""
import logging
import os
import uuid
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi import status as http_status

import storage
from models import (
    ORDER_STATUSES,
    FulfilmentMessage,
    Order,
    OrderCreateRequest,
    OrderListResponse,
    OrderStatusEvent,
    OrderStatusUpdateRequest,
)
from storage import (
    AwsEventPublisher,
    DynamoOrderRepository,
    EventPublisher,
    OrderRepository,
    StorageError,
)

LOGGER = logging.getLogger("order_processing_service")

app = FastAPI(
    title="Order Processing Service",
    version="1.0.0",
    description="Accepts orders, persists them to DynamoDB, enqueues fulfilment work and notifies subscribers.",
)

_repository: Optional[OrderRepository] = None
_publisher: Optional[EventPublisher] = None


def get_repository() -> OrderRepository:
    """Return the process-wide order repository (lazily constructed)."""
    global _repository
    if _repository is None:
        _repository = DynamoOrderRepository()
    return _repository


def get_publisher() -> EventPublisher:
    """Return the process-wide event publisher (lazily constructed)."""
    global _publisher
    if _publisher is None:
        _publisher = AwsEventPublisher()
    return _publisher


def _bad_gateway(detail: str, exc: Exception) -> HTTPException:
    LOGGER.error("%s: %s", detail, exc)
    return HTTPException(status_code=http_status.HTTP_502_BAD_GATEWAY, detail=detail)


def _emit_creation_events(publisher: EventPublisher, order: Dict[str, Any]) -> None:
    """Send the fulfilment message and the initial status event.

    Messaging failures are logged but never fail the request: the order is
    already durably stored and can be replayed by operators.
    """
    message = FulfilmentMessage(
        order_id=order["order_id"],
        customer_id=order["customer_id"],
        total_amount=order["total_amount"],
        currency=order["currency"],
        status=order["status"],
        created_at=order["created_at"],
    )
    try:
        publisher.send_fulfilment_message(message.model_dump())
    except StorageError as exc:
        LOGGER.error("could not enqueue fulfilment message for %s: %s", order["order_id"], exc)

    event = OrderStatusEvent(
        order_id=order["order_id"],
        customer_id=order["customer_id"],
        previous_status=None,
        new_status=order["status"],
        reason="order created",
        changed_at=order["created_at"],
    )
    try:
        publisher.publish_status_event(event.model_dump())
    except StorageError as exc:
        LOGGER.error("could not publish status event for %s: %s", order["order_id"], exc)


@app.get("/health")
def health() -> Dict[str, Any]:
    """Report service liveness and the resolved AWS client configuration."""
    return {
        "status": "ok",
        "service": "order_processing_service",
        "aws": {
            "region": storage.aws_region(),
            "endpoint_url": storage.aws_endpoint_url(),
            "orders_table": storage.orders_table_name(),
            "customer_index": storage.customer_index_name(),
            "queue_name": storage.queue_name(),
            "queue_url_configured": bool(storage.queue_url_from_env()),
            "status_topic_configured": bool(storage.topic_arn()),
        },
    }


@app.post("/orders", response_model=Order, status_code=http_status.HTTP_201_CREATED)
def create_order(
    payload: OrderCreateRequest,
    repo: OrderRepository = Depends(get_repository),
    publisher: EventPublisher = Depends(get_publisher),
) -> Order:
    """Persist a new order, then enqueue fulfilment work and notify subscribers."""
    now = storage.utc_now()
    items = [item.model_dump() for item in payload.items]
    total = round(sum(float(item["quantity"]) * float(item["unit_price"]) for item in items), 2)
    order: Dict[str, Any] = {
        "order_id": str(uuid.uuid4()),
        "customer_id": payload.customer_id,
        "status": "PENDING",
        "items": items,
        "total_amount": total,
        "currency": payload.currency.upper(),
        "shipping_address": payload.shipping_address,
        "notes": payload.notes,
        "created_at": now,
        "updated_at": now,
    }
    try:
        repo.create_order(order)
    except StorageError as exc:
        raise _bad_gateway("Could not persist order", exc) from exc

    _emit_creation_events(publisher, order)
    return Order(**order)


@app.get("/orders", response_model=OrderListResponse)
def list_orders(
    customer_id: str = Query(..., min_length=1, description="Customer identifier"),
    order_status: Optional[str] = Query(None, alias="status", description="Optional status filter"),
    limit: int = Query(25, ge=1, le=100, description="Maximum number of orders returned"),
    repo: OrderRepository = Depends(get_repository),
) -> OrderListResponse:
    """List a customer's orders, newest first, using the customer GSI."""
    if order_status is not None and order_status not in ORDER_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="status must be one of: " + ", ".join(ORDER_STATUSES),
        )
    try:
        rows = repo.list_orders_by_customer(customer_id, status=order_status, limit=limit)
    except StorageError as exc:
        raise _bad_gateway("Could not list orders", exc) from exc

    orders = [Order(**row) for row in rows]
    return OrderListResponse(customer_id=customer_id, count=len(orders), orders=orders)


@app.get("/orders/{order_id}", response_model=Order)
def get_order(
    order_id: str,
    repo: OrderRepository = Depends(get_repository),
) -> Order:
    """Fetch a single order by its primary key."""
    try:
        row = repo.get_order(order_id)
    except StorageError as exc:
        raise _bad_gateway("Could not read order", exc) from exc
    if row is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Order not found")
    return Order(**row)


@app.patch("/orders/{order_id}/status", response_model=Order)
def update_order_status(
    order_id: str,
    payload: OrderStatusUpdateRequest,
    repo: OrderRepository = Depends(get_repository),
    publisher: EventPublisher = Depends(get_publisher),
) -> Order:
    """Update an order's status and publish the change to SNS."""
    try:
        existing = repo.get_order(order_id)
    except StorageError as exc:
        raise _bad_gateway("Could not read order", exc) from exc
    if existing is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Order not found")

    try:
        updated = repo.update_status(order_id, payload.status, reason=payload.reason)
    except StorageError as exc:
        raise _bad_gateway("Could not update order status", exc) from exc
    if updated is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Order not found")

    event = OrderStatusEvent(
        order_id=order_id,
        customer_id=updated.get("customer_id", existing.get("customer_id", "")),
        previous_status=existing.get("status"),
        new_status=payload.status,
        reason=payload.reason,
        changed_at=updated.get("updated_at", storage.utc_now()),
    )
    try:
        publisher.publish_status_event(event.model_dump())
    except StorageError as exc:
        LOGGER.error("could not publish status event for %s: %s", order_id, exc)

    return Order(**updated)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
