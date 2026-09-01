"""Pydantic models used by the notification hub API."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Channel(str, Enum):
    """Supported delivery channels; unknown values are rejected with HTTP 422."""

    EMAIL = "email"
    WEBHOOK = "webhook"


def dump_model(model: BaseModel, exclude_unset: bool = False) -> Dict[str, Any]:
    """Return a plain dict for a pydantic model (works on pydantic v1 and v2)."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=exclude_unset)
    return model.dict(exclude_unset=exclude_unset)


class EventIn(BaseModel):
    """Event submitted by a producer service."""

    event_type: str = Field(..., min_length=1, max_length=128)
    subject: Optional[str] = Field(default=None, max_length=100)
    payload: Dict[str, Any] = Field(default_factory=dict)


class EventOut(BaseModel):
    """Event as published to SNS."""

    event_id: str
    event_type: str
    subject: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    published_at: str
    sns_message_id: str


class SubscriptionIn(BaseModel):
    """Subscription registration request."""

    channel: Channel
    target: str = Field(..., min_length=1, max_length=512)
    event_types: List[str] = Field(default_factory=list)
    active: bool = True


class SubscriptionUpdate(BaseModel):
    """Partial subscription update request."""

    target: Optional[str] = Field(default=None, max_length=512)
    event_types: Optional[List[str]] = None
    active: Optional[bool] = None


class SubscriptionOut(BaseModel):
    """Subscription record stored in DynamoDB."""

    subscription_id: str
    channel: str
    target: str
    event_types: List[str] = Field(default_factory=list)
    active: bool = True
    created_at: str
    updated_at: str


class SubscriptionListResponse(BaseModel):
    """Envelope for subscription listings."""

    count: int
    subscriptions: List[SubscriptionOut] = Field(default_factory=list)


class ChannelInfo(BaseModel):
    """A channel and the SQS queue backing it."""

    channel: str
    queue_url: str


class ChannelListResponse(BaseModel):
    """Envelope for the channel listing."""

    channels: List[ChannelInfo] = Field(default_factory=list)


class ChannelStats(BaseModel):
    """Per-channel SQS queue counters."""

    channel: str
    queue_url: str
    messages_available: int = 0
    messages_in_flight: int = 0
    messages_delayed: int = 0
    total_received: int = 0
    collected_at: str


class ChannelStatsResponse(BaseModel):
    """Envelope for the channel statistics report."""

    channels: List[ChannelStats] = Field(default_factory=list)


class QueueMessage(BaseModel):
    """A message received from a channel queue with the SNS envelope unwrapped."""

    message_id: str
    receipt_handle: str
    body: Dict[str, Any] = Field(default_factory=dict)
    attributes: Dict[str, Any] = Field(default_factory=dict)


class MessagesResponse(BaseModel):
    """Envelope for received channel messages."""

    channel: str
    count: int
    deleted: bool = False
    messages: List[QueueMessage] = Field(default_factory=list)


class HealthStatus(BaseModel):
    """Health probe response."""

    status: str
    checks: Dict[str, str] = Field(default_factory=dict)
