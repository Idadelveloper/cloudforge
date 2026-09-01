"""FastAPI application for the CloudForge event registration service.

Organisers create events (title, date, capacity) and attendees register for
them while capacity remains.  Events and registrations live in DynamoDB and
every successful registration is published to an SQS queue.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from storage import (
    DynamoRepository,
    EventFull,
    EventNotFound,
    Publisher,
    RegistrationNotFound,
    Repository,
    SqsPublisher,
    build_registration_message,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("event_registration_service")

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(
    title="Event Registration Service",
    description="Create events and register attendees until capacity is reached.",
    version="1.0.0",
)

_repository: Optional[Repository] = None
_publisher: Optional[Publisher] = None


def get_repository() -> Repository:
    """Return the process-wide repository (lazily created DynamoDB backend)."""
    global _repository
    if _repository is None:
        _repository = DynamoRepository()
    return _repository


def get_publisher() -> Publisher:
    """Return the process-wide publisher (lazily created SQS backend)."""
    global _publisher
    if _publisher is None:
        _publisher = SqsPublisher()
    return _publisher


class EventCreate(BaseModel):
    """Payload sent by an organiser to create an event."""

    title: str = Field(..., min_length=1, max_length=200)
    date: str = Field(..., min_length=1, max_length=64)
    capacity: int = Field(..., gt=0)


class EventOut(BaseModel):
    """Event representation returned by the API."""

    event_id: str
    title: str
    date: str
    capacity: int
    registered_count: int
    remaining_capacity: int
    created_at: str


class RegistrationCreate(BaseModel):
    """Payload sent by an attendee to register for an event."""

    attendee_name: str = Field(..., min_length=1, max_length=200)
    attendee_email: str = Field(..., min_length=3, max_length=254)


class RegistrationOut(BaseModel):
    """Registration representation returned by the API."""

    registration_id: str
    event_id: str
    attendee_name: str
    attendee_email: str
    status: str
    created_at: str


class HealthOut(BaseModel):
    """Health probe payload."""

    status: str
    events_table: str
    registrations_table: str
    queue: str
    aws_endpoint_url: Optional[str] = None


def _event_out(event: Dict[str, Any]) -> Dict[str, Any]:
    capacity = int(event.get("capacity", 0))
    registered = int(event.get("registered_count", 0))
    return {
        "event_id": event["event_id"],
        "title": event.get("title", ""),
        "date": event.get("date", ""),
        "capacity": capacity,
        "registered_count": registered,
        "remaining_capacity": max(capacity - registered, 0),
        "created_at": event.get("created_at", ""),
    }


def _validated_email(raw_email: str) -> str:
    email = raw_email.strip().lower()
    if not EMAIL_PATTERN.match(email):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="attendee_email is not a valid e-mail address",
        )
    return email


@app.get("/health", response_model=HealthOut)
def health(
    repo: Repository = Depends(get_repository),
    publisher: Publisher = Depends(get_publisher),
) -> Dict[str, Any]:
    """Report service status and the configured AWS resource names."""
    return {
        "status": "ok",
        "events_table": getattr(repo, "events_table_name", "in-memory"),
        "registrations_table": getattr(repo, "registrations_table_name", "in-memory"),
        "queue": getattr(publisher, "queue_name", "in-memory"),
        "aws_endpoint_url": os.environ.get("AWS_ENDPOINT_URL") or None,
    }


@app.post("/events", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(
    payload: EventCreate,
    repo: Repository = Depends(get_repository),
) -> Dict[str, Any]:
    """Create a new event."""
    title = payload.title.strip()
    date = payload.date.strip()
    if not title or not date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="title and date must not be blank",
        )
    event = repo.create_event(title=title, date=date, capacity=payload.capacity)
    logger.info("created event %s (capacity=%s)", event["event_id"], event["capacity"])
    return _event_out(event)


@app.get("/events", response_model=List[EventOut])
def list_events(repo: Repository = Depends(get_repository)) -> List[Dict[str, Any]]:
    """List every known event."""
    return [_event_out(event) for event in repo.list_events()]


@app.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: str, repo: Repository = Depends(get_repository)) -> Dict[str, Any]:
    """Fetch a single event."""
    try:
        event = repo.get_event(event_id)
    except EventNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _event_out(event)


@app.post(
    "/events/{event_id}/registrations",
    response_model=RegistrationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_registration(
    event_id: str,
    payload: RegistrationCreate,
    repo: Repository = Depends(get_repository),
    publisher: Publisher = Depends(get_publisher),
) -> Dict[str, Any]:
    """Register an attendee for an event while capacity remains."""
    email = _validated_email(payload.attendee_email)
    name = payload.attendee_name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="attendee_name must not be blank",
        )

    try:
        repo.get_event(event_id)
    except EventNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if repo.find_registration_by_email(event_id, email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="attendee is already registered for this event",
        )

    try:
        event = repo.reserve_capacity(event_id)
    except EventNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EventFull as exc:
        logger.info("rejected registration for full event %s", event_id)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    try:
        registration = repo.create_registration(event_id, name, email)
    except Exception as exc:  # pragma: no cover - defensive rollback path
        repo.release_capacity(event_id)
        logger.exception("failed to persist registration for event %s", event_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to store registration",
        ) from exc

    try:
        publisher.publish(build_registration_message(event, registration))
    except Exception:
        logger.exception(
            "failed to publish registration message for %s", registration["registration_id"]
        )

    logger.info(
        "registered %s for event %s (%s/%s)",
        registration["registration_id"],
        event_id,
        event.get("registered_count"),
        event.get("capacity"),
    )
    return registration


@app.get("/events/{event_id}/registrations", response_model=List[RegistrationOut])
def list_registrations(
    event_id: str,
    repo: Repository = Depends(get_repository),
) -> List[Dict[str, Any]]:
    """List all registrations recorded for an event."""
    try:
        repo.get_event(event_id)
    except EventNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return repo.list_registrations(event_id)


@app.get("/registrations/{registration_id}", response_model=RegistrationOut)
def get_registration(
    registration_id: str,
    repo: Repository = Depends(get_repository),
) -> Dict[str, Any]:
    """Fetch a single registration by its identifier."""
    try:
        return repo.get_registration(registration_id)
    except RegistrationNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
