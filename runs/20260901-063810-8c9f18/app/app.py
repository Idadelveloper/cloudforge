"""Product feedback service: FastAPI application entrypoint and routes.

Endpoints:
    POST /feedback              submit feedback (SNS alert when rating <= 2)
    GET  /feedback              list feedback with optional filters
    GET  /feedback/stats        aggregate rating statistics
    GET  /feedback/{id}         fetch a single feedback record
    GET  /health                liveness / readiness probe
"""
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from storage import DynamoFeedbackRepository, SnsNotifier

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("product_feedback_service")

DEFAULT_PRODUCT_ID = os.environ.get("DEFAULT_PRODUCT_ID", "general")
LOW_RATING_THRESHOLD = int(os.environ.get("LOW_RATING_THRESHOLD", "2"))
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class FeedbackCreateRequest(BaseModel):
    """Payload accepted by POST /feedback."""

    product_id: Optional[str] = Field(default=None, max_length=128)
    rating: int = Field(..., ge=1, le=5)
    comment: str = Field(..., min_length=1, max_length=2000)
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


class FeedbackListResponse(BaseModel):
    """Envelope returned by GET /feedback."""

    items: List[Feedback]
    count: int


class FeedbackStats(BaseModel):
    """Aggregate rating statistics."""

    product_id: Optional[str] = None
    total_count: int
    average_rating: float
    rating_distribution: Dict[str, int]


class HealthResponse(BaseModel):
    """Health probe payload."""

    status: str
    dynamodb: bool
    sns: bool


app = FastAPI(
    title="product_feedback_service",
    description="Collect product feedback, store it in DynamoDB and alert support on low ratings via SNS.",
    version="1.0.0",
)

_REPOSITORY: Optional[DynamoFeedbackRepository] = None
_NOTIFIER: Optional[SnsNotifier] = None


def get_repository() -> DynamoFeedbackRepository:
    """Return the process-wide feedback repository (lazily created)."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = DynamoFeedbackRepository()
    return _REPOSITORY


def get_notifier() -> SnsNotifier:
    """Return the process-wide SNS notifier (lazily created)."""
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = SnsNotifier()
    return _NOTIFIER


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _empty_distribution() -> Dict[str, int]:
    return {str(value): 0 for value in range(1, 6)}


@app.post("/feedback", response_model=Feedback, status_code=201)
def create_feedback(
    payload: FeedbackCreateRequest,
    repository: Any = Depends(get_repository),
    notifier: Any = Depends(get_notifier),
) -> Dict[str, Any]:
    """Store a new feedback record and alert support staff when the rating is low."""
    product_id = (payload.product_id or "").strip() or DEFAULT_PRODUCT_ID
    item: Dict[str, Any] = {
        "feedback_id": str(uuid.uuid4()),
        "product_id": product_id,
        "rating": int(payload.rating),
        "comment": payload.comment,
        "customer_email": payload.customer_email,
        "created_at": _utc_now_iso(),
        "alert_sent": False,
    }

    try:
        repository.put_feedback(item)
    except Exception as exc:  # pragma: no cover - depends on AWS availability
        LOGGER.error("Failed to persist feedback: %s", exc)
        raise HTTPException(status_code=503, detail="Could not store feedback") from exc

    if item["rating"] <= LOW_RATING_THRESHOLD:
        alert = {
            "feedback_id": item["feedback_id"],
            "product_id": item["product_id"],
            "rating": item["rating"],
            "comment": item["comment"],
            "created_at": item["created_at"],
        }
        item["alert_sent"] = bool(notifier.publish_low_rating(alert))
        if item["alert_sent"]:
            try:
                repository.put_feedback(item)
            except Exception as exc:  # pragma: no cover - best effort flag update
                LOGGER.warning("Stored feedback but could not update alert_sent flag: %s", exc)
        else:
            LOGGER.warning("Low rating alert could not be published for %s", item["feedback_id"])

    LOGGER.info(
        "Stored feedback %s (product=%s rating=%s alert_sent=%s)",
        item["feedback_id"],
        item["product_id"],
        item["rating"],
        item["alert_sent"],
    )
    return item


@app.get("/feedback", response_model=FeedbackListResponse)
def list_feedback(
    product_id: Optional[str] = Query(default=None, max_length=128),
    min_rating: Optional[int] = Query(default=None, ge=1, le=5),
    max_rating: Optional[int] = Query(default=None, ge=1, le=5),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    repository: Any = Depends(get_repository),
) -> Dict[str, Any]:
    """List feedback records, newest first, with optional filters."""
    if min_rating is not None and max_rating is not None and min_rating > max_rating:
        raise HTTPException(status_code=400, detail="min_rating cannot be greater than max_rating")

    try:
        items = repository.list_feedback(
            product_id=product_id,
            min_rating=min_rating,
            max_rating=max_rating,
            limit=limit,
        )
    except Exception as exc:  # pragma: no cover - depends on AWS availability
        LOGGER.error("Failed to list feedback: %s", exc)
        raise HTTPException(status_code=503, detail="Could not list feedback") from exc

    return {"items": items, "count": len(items)}


@app.get("/feedback/stats", response_model=FeedbackStats)
def feedback_stats(
    product_id: Optional[str] = Query(default=None, max_length=128),
    repository: Any = Depends(get_repository),
) -> Dict[str, Any]:
    """Return total count, average rating and per-star distribution."""
    try:
        items = repository.all_feedback(product_id=product_id)
    except Exception as exc:  # pragma: no cover - depends on AWS availability
        LOGGER.error("Failed to compute feedback stats: %s", exc)
        raise HTTPException(status_code=503, detail="Could not compute statistics") from exc

    distribution = _empty_distribution()
    total = 0
    rating_sum = 0
    for item in items:
        try:
            rating = int(item.get("rating", 0))
        except (TypeError, ValueError):
            continue
        if rating < 1 or rating > 5:
            continue
        distribution[str(rating)] += 1
        rating_sum += rating
        total += 1

    average = round(rating_sum / total, 2) if total else 0.0
    return {
        "product_id": product_id,
        "total_count": total,
        "average_rating": average,
        "rating_distribution": distribution,
    }


@app.get("/feedback/{feedback_id}", response_model=Feedback)
def get_feedback(
    feedback_id: str,
    repository: Any = Depends(get_repository),
) -> Dict[str, Any]:
    """Fetch a single feedback record by identifier."""
    try:
        item = repository.get_feedback(feedback_id)
    except Exception as exc:  # pragma: no cover - depends on AWS availability
        LOGGER.error("Failed to fetch feedback %s: %s", feedback_id, exc)
        raise HTTPException(status_code=503, detail="Could not fetch feedback") from exc

    if not item:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return item


@app.get("/health", response_model=HealthResponse)
def health(
    repository: Any = Depends(get_repository),
    notifier: Any = Depends(get_notifier),
) -> Dict[str, Any]:
    """Report service status and dependency reachability."""
    table_ok = bool(repository.ping())
    topic_ok = bool(notifier.ping())
    status = "ok" if table_ok and topic_ok else "degraded"
    return {"status": status, "dynamodb": table_ok, "sns": topic_ok}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),  # nosec B104 - container needs external binding
        port=int(os.environ.get("PORT", "8000")),
    )
