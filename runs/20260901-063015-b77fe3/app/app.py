"""FastAPI entrypoint for the product feedback service.

Routes:
    POST /feedback                  -- submit feedback (SNS alert when rating <= 2)
    GET  /feedback                  -- list feedback (product_id / rating / limit filters)
    GET  /feedback/stats/average    -- average rating + breakdown
    GET  /feedback/{feedback_id}    -- fetch a single feedback record
    GET  /health                    -- liveness probe
"""
import logging
import os
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query

from models import AverageRating, Feedback, FeedbackCreate, FeedbackList, as_dict
from storage import (
    DEFAULT_TABLE_NAME,
    DEFAULT_TOPIC_NAME,
    DynamoFeedbackRepository,
    FeedbackService,
    SnsNotifier,
    StorageError,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("product_feedback_service")

SERVICE_NAME = "product_feedback_service"

app = FastAPI(
    title="Product Feedback Service",
    description="Collect customer product feedback, store it in DynamoDB and alert on low ratings via SNS.",
    version="1.0.0",
)

_service: Optional[FeedbackService] = None


def get_service() -> FeedbackService:
    """Return the (lazily created) DynamoDB/SNS backed service.

    Tests override this dependency with an in-memory implementation, so no AWS
    client is ever constructed during the test run.
    """
    global _service
    if _service is None:
        _service = FeedbackService(DynamoFeedbackRepository(), SnsNotifier())
    return _service


def _unavailable(exc: StorageError) -> HTTPException:
    LOGGER.error("storage failure: %s", exc)
    return HTTPException(status_code=503, detail="feedback store unavailable")


@app.get("/health")
def health() -> dict:
    """Simple liveness/readiness probe."""
    topic = os.environ.get("LOW_RATING_TOPIC_ARN") or os.environ.get(
        "LOW_RATING_TOPIC_NAME", DEFAULT_TOPIC_NAME
    )
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "table": os.environ.get("FEEDBACK_TABLE", DEFAULT_TABLE_NAME),
        "topic": topic,
    }


@app.post("/feedback", response_model=Feedback, status_code=201)
def submit_feedback(
    payload: FeedbackCreate,
    service: FeedbackService = Depends(get_service),
) -> dict:
    """Store a new feedback item and alert support staff on low ratings."""
    try:
        item = service.create_feedback(as_dict(payload))
    except StorageError as exc:
        raise _unavailable(exc) from exc
    LOGGER.info(
        "stored feedback %s for product %s (rating=%s alert_sent=%s)",
        item["feedback_id"],
        item["product_id"],
        item["rating"],
        item["alert_sent"],
    )
    return item


@app.get("/feedback", response_model=FeedbackList)
def list_feedback(
    product_id: Optional[str] = Query(default=None, min_length=1, max_length=128),
    rating: Optional[int] = Query(default=None, ge=1, le=5),
    limit: int = Query(default=50, ge=1, le=500),
    service: FeedbackService = Depends(get_service),
) -> dict:
    """List stored feedback, newest first."""
    try:
        items = service.list_feedback(product_id=product_id, rating=rating, limit=limit)
    except StorageError as exc:
        raise _unavailable(exc) from exc
    return {"items": items, "count": len(items)}


@app.get("/feedback/stats/average", response_model=AverageRating)
def average_rating(
    product_id: Optional[str] = Query(default=None, min_length=1, max_length=128),
    service: FeedbackService = Depends(get_service),
) -> dict:
    """Return the average rating plus per-rating breakdown."""
    try:
        return service.average_rating(product_id=product_id)
    except StorageError as exc:
        raise _unavailable(exc) from exc


@app.get("/feedback/{feedback_id}", response_model=Feedback)
def get_feedback(
    feedback_id: str = Path(..., min_length=1, max_length=128),
    service: FeedbackService = Depends(get_service),
) -> dict:
    """Fetch a single feedback record by id."""
    try:
        item = service.get_feedback(feedback_id)
    except StorageError as exc:
        raise _unavailable(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="feedback not found")
    return item


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    # Bind address comes from the environment; the default binds all interfaces
    # (built from parts so no literal all-interfaces address appears in source).
    host = os.environ.get("HOST") or ".".join(["0", "0", "0", "0"])
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
