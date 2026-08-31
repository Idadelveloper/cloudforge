"""Pydantic request/response models for the personal notes API."""
from typing import List, Optional

from pydantic import BaseModel, Field

TITLE_MAX = 200
BODY_MAX = 20000


class NoteCreateRequest(BaseModel):
    """Payload accepted by POST /notes."""

    title: str = Field(..., min_length=1, max_length=TITLE_MAX)
    body: str = Field(default="", max_length=BODY_MAX)


class NoteUpdateRequest(BaseModel):
    """Payload accepted by PUT /notes/{note_id} (partial update)."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=TITLE_MAX)
    body: Optional[str] = Field(default=None, max_length=BODY_MAX)


class Note(BaseModel):
    """A stored note."""

    user_id: str
    note_id: str
    title: str
    body: str = ""
    created_at: str
    updated_at: str


class NoteListResponse(BaseModel):
    """Paginated collection of notes."""

    items: List[Note] = Field(default_factory=list)
    next_cursor: Optional[str] = None
    count: int = 0


class ErrorResponse(BaseModel):
    """Uniform error envelope."""

    detail: str
    code: str
