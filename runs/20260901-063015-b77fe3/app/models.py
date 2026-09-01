"""Pydantic request/response models for the product feedback service."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def as_dict(model: BaseModel) -> Dict[str, Any]:
    """Return a plain dict for a pydantic model (v1 and v2 compatible)."""
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        return dump()
    return model.dict()


class FeedbackCreate(BaseModel):
    """Incoming feedback submission."""

    product_id: str = Field(..., min_length=1, max_length=128)
    rating: int = Field(..., ge=1, le=5)
    comment: str = Field(..., min_length=1, max_length=4000)
    customer_email: Optional[str] = Field(default=None, max_length=320)


class Feedback(BaseModel):
    """A stored feedback record."""

    feedback_id: str
    product_id: str
    rating: int
    comment: str
    customer_email: Optional[str] = None
    created_at: str
    alert_sent: bool = False


class FeedbackList(BaseModel):
    """Envelope for feedback listings."""

    items: List[Feedback] = Field(default_factory=list)
    count: int = 0


class AverageRating(BaseModel):
    """Aggregate rating statistics."""

    product_id: Optional[str] = None
    average_rating: float = 0.0
    count: int = 0
    rating_breakdown: Dict[str, int] = Field(default_factory=dict)


class LowRatingAlert(BaseModel):
    """Payload published to SNS when a low rating arrives."""

    feedback_id: str
    product_id: str
    rating: int
    comment: str
    created_at: str
