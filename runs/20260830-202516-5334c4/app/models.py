"""Pydantic request/response models for the personal notes API."""
from typing import List, Optional

from pydantic import BaseModel, Field

TITLE_MAX_LENGTH = 200
BODY_MAX_LENGTH = 20000


class NoteCreateRequest(BaseModel):
    """Payload accepted by ``POST /notes``."""

    title: str = Field(..., min_length=1, max_length=TITLE_MAX_LENGTH)
    body: str = Field(..., max_length=BODY_MAX_LENGTH)


class NoteUpdateRequest(BaseModel):
    """Payload accepted by ``PUT /notes/{note_id}``."""

    title: str = Field(..., min_length=1, max_length=TITLE_MAX_LENGTH)
    body: str = Field(..., max_length=BODY_MAX_LENGTH)


class Note(BaseModel):
    """A stored note as returned by the API."""

    note_id: str
    owner_id: str
    title: str
    body: str
    created_at: str
    updated_at: str


class NoteListResponse(BaseModel):
    """Paginated collection of notes."""

    items: List[Note] = Field(default_factory=list)
    count: int = 0
    next_token: Optional[str] = None


class ErrorResponse(BaseModel):
    """Uniform error payload."""

    detail: str
    code: str
