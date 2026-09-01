"""Pydantic request/response models for the shop inventory API."""

from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator


class Product(BaseModel):
    """A stored product record."""

    sku: str
    name: str
    price: Decimal
    quantity: int
    created_at: str
    updated_at: str

    @field_serializer("price")
    def _serialize_price(self, value: Decimal) -> float:
        return float(value)


class ProductCreateRequest(BaseModel):
    """Payload for creating a product."""

    sku: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    price: Decimal = Field(..., ge=0, max_digits=12, decimal_places=2)
    quantity: int = Field(0, ge=0, le=1_000_000_000)

    @field_validator("sku", "name")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned


class StockAdjustmentRequest(BaseModel):
    """Payload for a signed stock adjustment."""

    delta: int = Field(..., description="Non-zero signed change to apply to quantity")
    reason: Optional[str] = Field(None, max_length=500)

    @field_validator("delta")
    @classmethod
    def _non_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("delta must be a non-zero integer")
        if abs(value) > 1_000_000_000:
            raise ValueError("delta magnitude is too large")
        return value


class ProductListResponse(BaseModel):
    """Paginated list of products."""

    items: List[Product]
    next_cursor: Optional[str] = None
    count: int


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str
    code: str
