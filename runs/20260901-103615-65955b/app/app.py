"""FastAPI notification hub.

Services publish events to a central SNS topic; subscribers register through this
REST API choosing a channel (email-like or webhook-like).  Each channel is backed
by its own SQS queue subscribed to the topic and subscription records live in
DynamoDB.  A stats endpoint reports how many messages every channel queue has
received.
"""
import logging
import os
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query

from models import EventPublishRequest, SubscriptionCreateRequest
from storage import CHANNELS, AwsNotificationRepository, utcnow_iso

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("notification_hub")

app = FastAPI(
    title="Notification Hub",
    version="1.0.0",
    description="SNS fan-out to per-channel SQS queues with DynamoDB backed subscriptions.",
)

_repository: Optional[AwsNotificationRepository] = None


def get_repository() -> AwsNotificationRepository:
    """Return the process wide repository (lazily built so imports stay offline)."""
    global _repository
    if _repository is None:
        _repository = AwsNotificationRepository()
    return _repository


def _validate_channel(channel: str) -> str:
    if channel not in CHANNELS:
        raise HTTPException(
            status_code=400,
            detail="unsupported channel '{}'; expected one of {}".format(channel, list(CHANNELS)),
        )
    return channel


def _call(func, *args, **kwargs) -> Any:
    """Invoke a repository method translating backend failures into HTTP 502."""
    try:
        return func(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any AWS/client failure as 502
        LOGGER.warning("aws operation failed: %s", exc.__class__.__name__)
        raise HTTPException(
            status_code=502,
            detail="aws backend error: {}".format(exc.__class__.__name__),
        ) from exc


@app.get("/health")
def health(repo: Any = Depends(get_repository)) -> dict:
    """Liveness check that also probes SNS, SQS and DynamoDB connectivity."""
    try:
        checks = repo.health()
    except Exception as exc:  # noqa: BLE001 - health must never raise
        checks = {"error": exc.__class__.__name__}
    healthy = bool(checks) and all(value == "ok" for value in checks.values())
    return {
        "status": "ok" if healthy else "degraded",
        "app": "notification_hub",
        "checks": checks,
        "checked_at": utcnow_iso(),
    }


@app.post("/subscriptions", status_code=201)
def create_subscription(
    payload: SubscriptionCreateRequest,
    repo: Any = Depends(get_repository),
) -> dict:
    """Register a subscriber and make sure its channel queue is wired to the topic."""
    _validate_channel(payload.channel)
    target = payload.target.strip()
    if not target:
        raise HTTPException(status_code=400, detail="target must not be empty")
    item = _call(
        repo.create_subscription,
        channel=payload.channel,
        target=target,
        event_types=payload.event_types,
    )
    return {"subscription": item}


@app.get("/subscriptions")
def list_subscriptions(
    channel: Optional[str] = Query(None),
    target: Optional[str] = Query(None),
    repo: Any = Depends(get_repository),
) -> dict:
    """List subscriptions, optionally filtered by channel or target endpoint."""
    if channel is not None:
        _validate_channel(channel)
    items = _call(repo.list_subscriptions, channel=channel, target=target)
    return {"count": len(items), "subscriptions": items}


@app.get("/subscriptions/{subscription_id}")
def get_subscription(subscription_id: str, repo: Any = Depends(get_repository)) -> dict:
    """Fetch a single subscription record."""
    item = _call(repo.get_subscription, subscription_id)
    if not item:
        raise HTTPException(status_code=404, detail="subscription not found")
    return {"subscription": item}


@app.delete("/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: str, repo: Any = Depends(get_repository)) -> dict:
    """Delete a subscription record from DynamoDB."""
    deleted = _call(repo.delete_subscription, subscription_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="subscription not found")
    return {"deleted": True, "subscription_id": subscription_id, "deleted_at": utcnow_iso()}


@app.post("/events", status_code=202)
def publish_event(payload: EventPublishRequest, repo: Any = Depends(get_repository)) -> dict:
    """Publish an event to the central SNS topic so it fans out to the channel queues."""
    event_type = payload.event_type.strip()
    if not event_type:
        raise HTTPException(status_code=400, detail="event_type must not be empty")
    if payload.channel is not None:
        _validate_channel(payload.channel)
    return _call(
        repo.publish_event,
        event_type=event_type,
        payload=payload.payload,
        channel=payload.channel,
        subject=payload.subject,
    )


@app.get("/channels")
def list_channels(repo: Any = Depends(get_repository)) -> dict:
    """List supported channels together with their backing queue URLs/ARNs."""
    channels = _call(repo.list_channels)
    return {"count": len(channels), "channels": channels}


@app.get("/channels/{channel}/messages")
def read_channel_messages(
    channel: str,
    max_messages: int = Query(10, ge=1, le=10),
    delete: bool = Query(False),
    wait_seconds: int = Query(0, ge=0, le=20),
    repo: Any = Depends(get_repository),
) -> dict:
    """Receive (and optionally delete) messages from a channel queue."""
    _validate_channel(channel)
    messages = _call(
        repo.receive_messages,
        channel=channel,
        max_messages=max_messages,
        delete=delete,
        wait_seconds=wait_seconds,
    )
    return {
        "channel": channel,
        "deleted": delete,
        "count": len(messages),
        "messages": messages,
    }


@app.get("/stats")
def stats(repo: Any = Depends(get_repository)) -> dict:
    """Report per-channel queue metrics and subscription counts."""
    data = _call(repo.stats)
    result = dict(data)
    result["generated_at"] = utcnow_iso()
    return result


def main() -> None:  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
