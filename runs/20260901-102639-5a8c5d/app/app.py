"""FastAPI application entrypoint for the notification hub service.

Producers POST events to /events which are published to a central SNS topic.
Subscribers are registered through the /subscriptions CRUD endpoints and are
stored in DynamoDB.  Each supported channel ('email' and 'webhook') is backed
by its own SQS queue subscribed to the topic; the /channels endpoints expose
the queue URLs, per-channel message counters and the pending messages.
"""

import logging
import os
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response

from models import (
    Channel,
    ChannelInfo,
    ChannelListResponse,
    ChannelStats,
    ChannelStatsResponse,
    EventIn,
    EventOut,
    HealthStatus,
    MessagesResponse,
    QueueMessage,
    SubscriptionIn,
    SubscriptionOut,
    SubscriptionUpdate,
    dump_model,
)
from storage import CHANNELS, DependencyError, NotFoundError, NotificationRepository

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("notification_hub")

app = FastAPI(
    title="Notification Hub",
    version="1.0.0",
    description="Central SNS/SQS notification fan-out hub with DynamoDB backed subscriptions.",
)

_REPOSITORY = None


def get_repository():
    """Return the process wide repository, creating it on first use."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = NotificationRepository()
    return _REPOSITORY


def _guard(func, *args, **kwargs):
    """Call a repository function translating storage errors into HTTP errors."""
    try:
        return func(*args, **kwargs)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DependencyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _validate_target(channel: str, target: str) -> str:
    """Validate the destination target for the given channel."""
    cleaned = (target or "").strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="target must not be empty")
    if channel == "email" and "@" not in cleaned:
        raise HTTPException(status_code=422, detail="email channel requires an email-like target")
    if channel == "webhook" and not cleaned.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=422,
            detail="webhook channel requires a target starting with http:// or https://",
        )
    return cleaned


@app.get("/health", response_model=HealthStatus)
def health(response: Response, repo=Depends(get_repository)) -> HealthStatus:
    """Report reachability of SNS, SQS and DynamoDB."""
    try:
        checks = repo.health()
    except Exception as exc:  # pragma: no cover - defensive
        checks = {"service": "error: {}".format(exc)}
    status = "ok" if checks and all(value == "ok" for value in checks.values()) else "degraded"
    if status != "ok":
        response.status_code = 503
    return HealthStatus(status=status, checks=checks)


@app.post("/events", response_model=EventOut, status_code=201)
def publish_event(event: EventIn, repo=Depends(get_repository)) -> EventOut:
    """Publish an event to the central SNS topic."""
    result = _guard(repo.publish_event, event.event_type, event.subject, event.payload)
    LOGGER.info("published event id=%s type=%s", result.get("event_id"), result.get("event_type"))
    return EventOut(**result)


@app.post("/subscriptions", response_model=SubscriptionOut, status_code=201)
def create_subscription(payload: SubscriptionIn, repo=Depends(get_repository)) -> SubscriptionOut:
    """Register a subscriber for one of the supported channels."""
    channel = payload.channel.value
    target = _validate_target(channel, payload.target)
    item = _guard(
        repo.create_subscription,
        channel,
        target,
        list(payload.event_types or []),
        bool(payload.active),
    )
    LOGGER.info("created subscription id=%s channel=%s", item.get("subscription_id"), channel)
    return SubscriptionOut(**item)


@app.get("/subscriptions", response_model=ChannelStatsResponse.__fields__ and None or None)
def _unused() -> None:  # pragma: no cover - placeholder removed below
    return None


@app.get("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
def get_subscription(subscription_id: str, repo=Depends(get_repository)) -> SubscriptionOut:
    """Fetch a single subscription by id."""
    item = _guard(repo.get_subscription, subscription_id)
    return SubscriptionOut(**item)


@app.patch("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
def update_subscription(
    subscription_id: str,
    payload: SubscriptionUpdate,
    repo=Depends(get_repository),
) -> SubscriptionOut:
    """Update the target, event_types filter or active flag of a subscription."""
    updates = dump_model(payload, exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="no updatable fields supplied")
    existing = _guard(repo.get_subscription, subscription_id)
    if "target" in updates and updates["target"] is not None:
        updates["target"] = _validate_target(existing.get("channel", ""), updates["target"])
    if "event_types" in updates and updates["event_types"] is None:
        updates["event_types"] = []
    if "active" in updates and updates["active"] is None:
        del updates["active"]
    item = _guard(repo.update_subscription, subscription_id, updates)
    LOGGER.info("updated subscription id=%s fields=%s", subscription_id, sorted(updates))
    return SubscriptionOut(**item)


@app.delete("/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: str, repo=Depends(get_repository)):
    """Delete a subscription record."""
    _guard(repo.delete_subscription, subscription_id)
    LOGGER.info("deleted subscription id=%s", subscription_id)
    return {"deleted": True, "subscription_id": subscription_id}


@app.get("/channels", response_model=ChannelListResponse)
def list_channels(repo=Depends(get_repository)) -> ChannelListResponse:
    """List the supported channels and their backing SQS queue URLs."""
    queue_urls = _guard(repo.channel_queue_urls)
    channels = [ChannelInfo(channel=name, queue_url=queue_urls.get(name, "")) for name in CHANNELS]
    return ChannelListResponse(channels=channels)


@app.get("/channels/stats", response_model=ChannelStatsResponse)
def channel_stats(repo=Depends(get_repository)) -> ChannelStatsResponse:
    """Report per-channel queue message counters."""
    stats = _guard(repo.all_channel_stats)
    return ChannelStatsResponse(channels=[ChannelStats(**row) for row in stats])


@app.get("/channels/{channel}/messages", response_model=MessagesResponse)
def channel_messages(
    channel: Channel,
    max_messages: int = Query(10, ge=1, le=10),
    wait_time_seconds: int = Query(0, ge=0, le=20),
    delete: bool = Query(False),
    repo=Depends(get_repository),
) -> MessagesResponse:
    """Receive (and optionally delete) pending messages from a channel queue."""
    messages = _guard(
        repo.receive_messages,
        channel.value,
        max_messages,
        wait_time_seconds,
        delete,
    )
    return MessagesResponse(
        channel=channel.value,
        count=len(messages),
        deleted=delete,
        messages=[QueueMessage(**message) for message in messages],
    )


def _list_subscriptions(channel: Optional[Channel], limit: int, repo):
    items = _guard(repo.list_subscriptions, channel.value if channel else None, limit)
    return [SubscriptionOut(**item) for item in items]


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
