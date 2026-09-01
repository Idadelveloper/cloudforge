"""Pydantic request/response models for the event registration service."""
import re
from datetime import date as date_type
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EventCreate(BaseModel):
    """Payload for creating an event."""

    title: str = Field(..., min_length=1, max_length=200)
    date: str = Field(..., description="ISO-8601 date, for example 2025-06-01")
    capacity: int = Field(..., ge=1, le=1000000)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title must not be blank")
        return cleaned

    @field_validator("date")
    @classmethod
    def _validate_date(cls, value: str) -> str:
        cleaned = value.strip()
        try:
            date_type.fromisoformat(cleaned)
        except ValueError as exc:
            raise ValueError("date must be an ISO-8601 date (YYYY-MM-DD)") from exc
        return cleaned


class RegistrationCreate(BaseModel):
    """Payload for registering an attendee."""

    attendee_name: str = Field(..., min_length=1, max_length=200)
    attendee_email: str = Field(..., min_length=3, max_length=320)

    @field_validator("attendee_name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("attendee_name must not be blank")
        return cleaned

    @field_validator("attendee_email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        cleaned = value.strip()
        if not EMAIL_PATTERN.match(cleaned):
            raise ValueError("attendee_email must be a valid email address")
        return cleaned


class EventResponse(BaseModel):
    """An event as returned by the API."""

    event_id: str
    title: str
    date: str
    capacity: int
    registered_count: int
    remaining_capacity: int
    created_at: str


class EventListResponse(BaseModel):
    """A page of events."""

    events: List[EventResponse] = Field(default_factory=list)
    next_cursor: Optional[str] = None


class RegistrationResponse(BaseModel):
    """A registration as returned by the API."""

    registration_id: str
    event_id: str
    attendee_name: str
    attendee_email: str
    status: str
    created_at: str


class RegistrationCreatedResponse(RegistrationResponse):
    """A freshly created registration plus queue/capacity metadata."""

    queued: bool = False
    remaining_capacity: int = 0


class RegistrationListResponse(BaseModel):
    """A page of registrations."""

    registrations: List[RegistrationResponse] = Field(default_factory=list)
    next_cursor: Optional[str] = None


class HealthResponse(BaseModel):
    """Health information for the service."""

    status: str
    service: str
    dependencies: Dict[str, str] = Field(default_factory=dict)
