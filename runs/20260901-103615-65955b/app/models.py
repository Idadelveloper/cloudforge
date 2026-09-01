"""Pydantic request models for the notification hub API."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SubscriptionCreateRequest(BaseModel):
    """Payload for POST /subscriptions."""

    channel: str
    target: str
    event_types: Optional[List[str]] = None


class EventPublishRequest(BaseModel):
    """Payload for POST /events."""

    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    channel: Optional[str] = None
    subject: Optional[str] = None
