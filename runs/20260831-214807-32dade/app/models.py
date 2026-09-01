"""Pydantic request/response models for the shop inventory API."""

from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ProductCreateRequest(BaseModel):
    """Payload accepted when creating a product."""

    sku: str = Field(..., min_length=1, max_length=64, description="Unique product identifier")
    name: str = Field(..., min_length=1, max_length=200, description="Product display name")
    price: Decimal = Field(..., ge=0, description="Unit price, non-negative")
    quantity: int = Field(0, ge=0, description="Initial on-hand stock count")


class StockAdjustmentRequest(BaseModel):
    """Payload accepted when adjusting stock up or down."""

    delta: int = Field(..., description="Signed, non-zero stock adjustment")
    reason: Optional[str] = Field(None, max_length=500, description="Optional free-text note")


class Product(BaseModel):
    """A stored product record."""

    sku: str
    name: str
    price: float
    quantity: int
    created_at: str
    updated_at: str


class ProductListResponse(BaseModel):
    """A page of products."""

    items: List[Product] = Field(default_factory=list)
    count: int = 0
    next_token: Optional[str] = None


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str
    code: str


class HealthResponse(BaseModel):
    """Health probe body."""

    status: str
    table: str
    dynamodb: str


def to_dict(model: BaseModel) -> Dict[str, Any]:
    """Dump a pydantic model to a plain dict (works with pydantic v1 and v2)."""
    dumper = getattr(model, "model_dump", None)
    if callable(dumper):
        return dumper()
    return model.dict()  # pragma: no cover - pydantic v1 fallback
