"""Pydantic request models for the order processing service.

Only plain field constraints are used so the models work with both pydantic v1
and pydantic v2.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    """A single line item of an order."""

    sku: str = Field(..., min_length=1)
    name: Optional[str] = None
    quantity: int = Field(..., ge=1)
    unit_price: float = Field(..., ge=0)


class CreateOrderRequest(BaseModel):
    """Payload accepted by ``POST /orders``."""

    customer_id: str = Field(..., min_length=1)
    items: List[OrderItem] = Field(default_factory=list)
    currency: Optional[str] = "USD"
    shipping_address: Optional[str] = None
    notes: Optional[str] = None


class OrderStatusUpdateRequest(BaseModel):
    """Payload accepted by ``PATCH /orders/{order_id}/status``."""

    status: str = Field(..., min_length=1)
    reason: Optional[str] = None
