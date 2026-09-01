"""FastAPI application for the event registration service.

Organisers create events (title, date, capacity); attendees register while
seats remain. Events and registrations live in DynamoDB and every successful
registration is published to an SQS queue for downstream processing.
"""
import logging
import os
from functools import lru_cache
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from models import (
    EventCreate,
    EventListResponse,
    EventResponse,
    HealthResponse,
    RegistrationCreate,
    RegistrationCreatedResponse,
    RegistrationListResponse,
    RegistrationResponse,
)
from storage import (
    DuplicateRegistrationError,
    DynamoDBRepository,
    EventFullError,
    EventNotFoundError,
    InvalidCursorError,
    SqsPublisher,
    StorageError,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("event_registration_service")

APP_NAME = "event-registration-service"
MAX_PAGE_SIZE = 100

app = FastAPI(
    title="Event Registration Service",
    version="1.0.0",
    description="Create events and register attendees until the event is full.",
)


@lru_cache(maxsize=1)
def get_repository() -> DynamoDBRepository:
    """Return the shared DynamoDB backed repository."""
    return DynamoDBRepository()


@lru_cache(maxsize=1)
def get_publisher() -> SqsPublisher:
    """Return the shared SQS publisher."""
    return SqsPublisher()


@app.exception_handler(StorageError)
async def _storage_error_handler(request, exc):  # pragma: no cover - defensive
    LOGGER.error("storage failure: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "storage backend unavailable"})


def _event_response(item: Dict[str, Any]) -> EventResponse:
    capacity = int(item.get("capacity", 0))
    registered = int(item.get("registered_count", 0))
    return EventResponse(
        event_id=str(item.get("event_id", "")),
        title=str(item.get("title", "")),
        date=str(item.get("date", "")),
        capacity=capacity,
        registered_count=registered,
        remaining_capacity=max(capacity - registered, 0),
        created_at=str(item.get("created_at", "")),
    )


def _registration_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "registration_id": str(item.get("registration_id", "")),
        "event_id": str(item.get("event_id", "")),
        "attendee_name": str(item.get("attendee_name", "")),
        "attendee_email": str(item.get("attendee_email", "")),
        "status": str(item.get("status", "confirmed")),
        "created_at": str(item.get("created_at", "")),
    }


@app.get("/health", response_model=HealthResponse)
def health(repo=Depends(get_repository), publisher=Depends(get_publisher)) -> HealthResponse:
    """Report liveness of the service and its dependencies."""
    dependencies = {
        "dynamodb": "ok" if repo.health() else "unavailable",
        "sqs": "ok" if publisher.health() else "unavailable",
    }
    overall = "ok" if all(value == "ok" for value in dependencies.values()) else "degraded"
    return HealthResponse(status=overall, service=APP_NAME, dependencies=dependencies)


@app.post("/events", response_model=EventResponse, status_code=status.HTTP_201_CREATED)
def create_event(payload: EventCreate, repo=Depends(get_repository)) -> EventResponse:
    """Create a new event."""
    item = repo.create_event(payload.title, payload.date, payload.capacity)
    LOGGER.info("created event %s", item.get("event_id"))
    return _event_response(item)


@app.get("/events", response_model=EventListResponse)
def list_events(
    limit: int = Query(MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(None),
    repo=Depends(get_repository),
) -> EventListResponse:
    """List events with their capacity and current registration count."""
    try:
        items, next_cursor = repo.list_events(limit=limit, cursor=cursor)
    except InvalidCursorError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return EventListResponse(events=[_event_response(i) for i in items], next_cursor=next_cursor)


@app.get("/events/{event_id}", response_model=EventResponse)
def get_event(event_id: str, repo=Depends(get_repository)) -> EventResponse:
    """Retrieve a single event including its remaining capacity."""
    item = repo.get_event(event_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"event '{event_id}' not found",
        )
    return _event_response(item)


@app.post(
    "/events/{event_id}/registrations",
    response_model=RegistrationCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_registration(
    event_id: str,
    payload: RegistrationCreate,
    repo=Depends(get_repository),
    publisher=Depends(get_publisher),
) -> RegistrationCreatedResponse:
    """Register an attendee for an event and publish the registration to SQS."""
    try:
        registration, event = repo.create_registration(
            event_id, payload.attendee_name, payload.attendee_email
        )
    except EventNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (EventFullError, DuplicateRegistrationError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    message = {
        "registration_id": registration.get("registration_id"),
        "event_id": event_id,
        "event_title": event.get("title", ""),
        "attendee_name": registration.get("attendee_name"),
        "attendee_email": registration.get("attendee_email"),
        "registered_at": registration.get("created_at"),
    }
    queued = publisher.publish(message)
    if not queued:
        LOGGER.warning(
            "registration %s stored but not published to the queue",
            registration.get("registration_id"),
        )

    capacity = int(event.get("capacity", 0))
    registered = int(event.get("registered_count", 0))
    payload_out = _registration_payload(registration)
    return RegistrationCreatedResponse(
        **payload_out,
        queued=queued,
        remaining_capacity=max(capacity - registered, 0),
    )


@app.get("/events/{event_id}/registrations", response_model=RegistrationListResponse)
def list_registrations(
    event_id: str,
    limit: int = Query(MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(None),
    repo=Depends(get_repository),
) -> RegistrationListResponse:
    """List the registrations recorded for an event."""
    if repo.get_event(event_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"event '{event_id}' not found",
        )
    try:
        items, next_cursor = repo.list_registrations(event_id, limit=limit, cursor=cursor)
    except InvalidCursorError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    registrations = [RegistrationResponse(**_registration_payload(i)) for i in items]
    return RegistrationListResponse(registrations=registrations, next_cursor=next_cursor)


def main() -> None:  # pragma: no cover - manual entrypoint
    """Run the service with uvicorn."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
