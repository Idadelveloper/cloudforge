"""Pydantic request models for the loyalty points service."""

from typing import Optional

from pydantic import BaseModel, Field


class CustomerCreate(BaseModel):
    """Payload for creating a loyalty account."""

    customer_id: Optional[str] = Field(default=None, max_length=128)
    email: str = Field(..., min_length=3, max_length=320)
    name: str = Field(..., min_length=1, max_length=200)


class PurchaseCreate(BaseModel):
    """Payload for submitting a purchase for point accrual."""

    customer_id: str = Field(..., min_length=1, max_length=128)
    order_id: str = Field(..., min_length=1, max_length=128)
    amount_cents: int = Field(..., ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    occurred_at: Optional[str] = Field(default=None, max_length=64)
    idempotency_key: Optional[str] = Field(default=None, max_length=200)
