"""Pydantic models for the personal notes API."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TITLE_LENGTH = 200
MAX_BODY_LENGTH = 40000


class NoteCreateRequest(BaseModel):
    """Payload accepted by ``POST /notes``."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=MAX_TITLE_LENGTH)
    body: str = Field(default="", max_length=MAX_BODY_LENGTH)


class NoteUpdateRequest(BaseModel):
    """Payload accepted by ``PUT /notes/{note_id}`` (partial update)."""

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=MAX_TITLE_LENGTH)
    body: Optional[str] = Field(default=None, max_length=MAX_BODY_LENGTH)

    @model_validator(mode="after")
    def ensure_at_least_one_field(self) -> "NoteUpdateRequest":
        """Reject updates that would not change anything."""
        if self.title is None and self.body is None:
            raise ValueError("at least one of 'title' or 'body' must be provided")
        return self


class Note(BaseModel):
    """A stored note as returned by the API."""

    model_config = ConfigDict(extra="ignore")

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


class HealthResponse(BaseModel):
    """Health probe payload."""

    status: str
    table: str
