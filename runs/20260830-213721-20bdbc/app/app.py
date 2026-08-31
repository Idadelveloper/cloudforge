"""FastAPI application exposing CRUD endpoints for personal notes.

The HTTP layer is fully decoupled from persistence: every route depends on the
``NotesRepository`` interface defined in :mod:`storage`, so tests can inject an
in-memory implementation and never touch AWS.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from models import HealthResponse, Note, NoteCreateRequest, NoteListResponse, NoteUpdateRequest
from storage import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DynamoDBNotesRepository,
    InvalidCursorError,
    NoteNotFoundError,
    NotesRepository,
    StorageError,
    notes_table_name,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("personal_notes_api")

app = FastAPI(
    title="personal_notes_api",
    version="1.0.0",
    description="REST API for personal notes backed by DynamoDB.",
)

_repository: Optional[NotesRepository] = None


def get_repository() -> NotesRepository:
    """Return the process-wide repository, creating the DynamoDB one on demand."""
    global _repository
    if _repository is None:
        _repository = DynamoDBNotesRepository()
    return _repository


def set_repository(repository: Optional[NotesRepository]) -> None:
    """Replace the process-wide repository (used by tests and bootstrapping)."""
    global _repository
    _repository = repository


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string ending in 'Z'."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@app.exception_handler(NoteNotFoundError)
async def note_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate a missing note into HTTP 404."""
    LOGGER.info("note not found: %s", exc)
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(InvalidCursorError)
async def invalid_cursor_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate a malformed pagination cursor into HTTP 400."""
    LOGGER.info("invalid cursor: %s", exc)
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


@app.exception_handler(StorageError)
async def storage_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate datastore failures into HTTP 500 without leaking internals."""
    LOGGER.error("storage failure on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "notes datastore is currently unavailable"},
    )


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health(repo: NotesRepository = Depends(get_repository)) -> HealthResponse:
    """Liveness/readiness probe: also verifies the DynamoDB table is reachable."""
    table = getattr(repo, "table_name", None) or notes_table_name()
    if not repo.healthy():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="notes datastore is not reachable",
        )
    return HealthResponse(status="ok", table=table)


@app.post("/notes", response_model=Note, status_code=status.HTTP_201_CREATED, tags=["notes"])
def create_note(
    payload: NoteCreateRequest,
    repo: NotesRepository = Depends(get_repository),
) -> Note:
    """Create a new note with a server generated id and timestamps."""
    now = _utc_now()
    item = {
        "note_id": str(uuid.uuid4()),
        "title": payload.title,
        "body": payload.body,
        "created_at": now,
        "updated_at": now,
    }
    stored = repo.create(item)
    LOGGER.info("created note %s", stored.get("note_id"))
    return Note.model_validate(stored)


@app.get("/notes", response_model=NoteListResponse, tags=["notes"])
def list_notes(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(None, max_length=4096),
    repo: NotesRepository = Depends(get_repository),
) -> NoteListResponse:
    """List notes with an opaque pagination cursor."""
    items, next_cursor = repo.list(limit=limit, cursor=cursor)
    notes = [Note.model_validate(item) for item in items]
    return NoteListResponse(items=notes, next_cursor=next_cursor, count=len(notes))


@app.get("/notes/{note_id}", response_model=Note, tags=["notes"])
def get_note(note_id: str, repo: NotesRepository = Depends(get_repository)) -> Note:
    """Fetch a single note by id."""
    return Note.model_validate(repo.get(note_id))


@app.put("/notes/{note_id}", response_model=Note, tags=["notes"])
def update_note(
    note_id: str,
    payload: NoteUpdateRequest,
    repo: NotesRepository = Depends(get_repository),
) -> Note:
    """Partially update a note and refresh its updated_at timestamp."""
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    updates["updated_at"] = _utc_now()
    stored = repo.update(note_id, updates)
    LOGGER.info("updated note %s", note_id)
    return Note.model_validate(stored)


@app.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["notes"])
def delete_note(note_id: str, repo: NotesRepository = Depends(get_repository)) -> Response:
    """Delete a note by id."""
    repo.delete(note_id)
    LOGGER.info("deleted note %s", note_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
