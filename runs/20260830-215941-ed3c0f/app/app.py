"""Personal Notes REST API.

A small FastAPI service exposing CRUD endpoints for personal notes that are
persisted in a single DynamoDB table.  The AWS layer lives in ``storage.py`` and
is injected through a FastAPI dependency so the HTTP layer can be tested
without any AWS access.
"""

import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from storage import DynamoNotesRepository, InvalidTokenError, NoteNotFound, utc_now_iso

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("personal_notes_api")

DEFAULT_LIMIT = 50
MAX_LIMIT = 100

app = FastAPI(
    title="Personal Notes API",
    version="1.0.0",
    description="REST API for creating, listing, fetching, updating and deleting personal notes.",
)


class Note(BaseModel):
    """A stored note."""

    id: str
    title: str
    body: str
    created_at: str
    updated_at: str


class NoteCreateRequest(BaseModel):
    """Payload accepted by ``POST /notes``."""

    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(default="")


class NoteUpdateRequest(BaseModel):
    """Payload accepted by ``PUT /notes/{note_id}`` (partial update)."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    body: Optional[str] = Field(default=None)


class NoteListResponse(BaseModel):
    """Paginated list of notes."""

    items: List[Note]
    next_token: Optional[str] = None
    count: int


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str
    code: Optional[str] = None


class HealthResponse(BaseModel):
    """Liveness/readiness payload."""

    status: str
    table: str
    dynamodb: str
    detail: Optional[str] = None


def _dump(model: BaseModel, **kwargs: Any) -> Dict[str, Any]:
    """Return a plain dict for a pydantic model (v1 and v2 compatible)."""
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    return model.dict(**kwargs)


_repository: Optional[DynamoNotesRepository] = None


def get_repository() -> DynamoNotesRepository:
    """Lazily build (and cache) the DynamoDB backed repository."""
    global _repository
    if _repository is None:
        _repository = DynamoNotesRepository()
    return _repository


@app.get("/health", response_model=HealthResponse)
def health(repo: DynamoNotesRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Report service status and DynamoDB table reachability."""
    reachable, detail = repo.health()
    return {
        "status": "ok",
        "table": repo.table_name,
        "dynamodb": "available" if reachable else "unavailable",
        "detail": detail,
    }


@app.post(
    "/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"model": ErrorResponse}},
)
def create_note(
    payload: NoteCreateRequest,
    repo: DynamoNotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Create a new note and return it."""
    now = utc_now_iso()
    note = {
        "id": str(uuid.uuid4()),
        "title": payload.title,
        "body": payload.body or "",
        "created_at": now,
        "updated_at": now,
    }
    created = repo.create(note)
    logger.info("note created id=%s", created["id"])
    return created


@app.get("/notes", response_model=NoteListResponse, responses={400: {"model": ErrorResponse}})
def list_notes(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    next_token: Optional[str] = Query(default=None),
    repo: DynamoNotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List notes with cursor based pagination."""
    try:
        items, token = repo.list_notes(limit=limit, next_token=next_token)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": items, "next_token": token, "count": len(items)}


@app.get("/notes/{note_id}", response_model=Note, responses={404: {"model": ErrorResponse}})
def get_note(
    note_id: str,
    repo: DynamoNotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Fetch a single note by id."""
    note = repo.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.put(
    "/notes/{note_id}",
    response_model=Note,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def update_note(
    note_id: str,
    payload: NoteUpdateRequest,
    repo: DynamoNotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Partially update an existing note."""
    fields = {k: v for k, v in _dump(payload, exclude_unset=True).items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="At least one of 'title' or 'body' must be provided")
    try:
        updated = repo.update(note_id, fields, utc_now_iso())
    except NoteNotFound as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    logger.info("note updated id=%s fields=%s", note_id, sorted(fields))
    return updated


@app.delete(
    "/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse}},
)
def delete_note(
    note_id: str,
    repo: DynamoNotesRepository = Depends(get_repository),
) -> Response:
    """Delete a note by id."""
    try:
        repo.delete(note_id)
    except NoteNotFound as exc:
        raise HTTPException(status_code=404, detail="Note not found") from exc
    logger.info("note deleted id=%s", note_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
