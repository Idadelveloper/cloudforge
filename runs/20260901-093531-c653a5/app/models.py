"""Pydantic models for the order-processing service."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

ORDER_STATUSES = ("PENDING", "PROCESSING", "FULFILLED", "CANCELLED", "FAILED")

OrderStatus = Literal["PENDING", "PROCESSING", "FULFILLED", "CANCELLED", "FAILED"]


class OrderItem(BaseModel):
    """A single line item of an order."""

    sku: str = Field(..., min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=512)
    quantity: int = Field(..., ge=1, le=10000)
    unit_price: float = Field(..., ge=0)


class OrderCreateRequest(BaseModel):
    """Payload accepted by POST /orders."""

    customer_id: str = Field(..., min_length=1, max_length=128)
    items: List[OrderItem] = Field(..., min_length=1)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    shipping_address: Optional[str] = Field(default=None, max_length=512)
    notes: Optional[str] = Field(default=None, max_length=1024)


class Order(BaseModel):
    """A stored order record."""

    order_id: str
    customer_id: str
    status: str
    items: List[OrderItem]
    total_amount: float
    currency: str
    shipping_address: Optional[str] = None
    notes: Optional[str] = None
    created_at: str
    updated_at: str


class OrderStatusUpdateRequest(BaseModel):
    """Payload accepted by PATCH /orders/{order_id}/status."""

    status: OrderStatus
    reason: Optional[str] = Field(default=None, max_length=512)


class OrderListResponse(BaseModel):
    """Response body for GET /orders."""

    customer_id: str
    count: int
    orders: List[Order]


class FulfilmentMessage(BaseModel):
    """Message body sent to the fulfilment SQS queue."""

    order_id: str
    customer_id: str
    total_amount: float
    currency: str
    status: str
    created_at: str


class OrderStatusEvent(BaseModel):
    """Event published to the order status SNS topic."""

    order_id: str
    customer_id: str
    previous_status: Optional[str] = None
    new_status: str
    reason: Optional[str] = None
    changed_at: str
