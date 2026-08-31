"""FastAPI application exposing CRUD endpoints for personal notes.

Routes:
    GET    /health            liveness probe
    POST   /notes             create a note
    GET    /notes             list notes (paginated)
    GET    /notes/{note_id}   fetch a single note
    PUT    /notes/{note_id}   replace title/body of a note
    DELETE /notes/{note_id}   delete a note

The persistence layer is injected through the ``get_repository`` dependency so
that tests can substitute an in-memory repository (no AWS access required).
"""
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from models import ErrorResponse, Note, NoteCreateRequest, NoteListResponse, NoteUpdateRequest
from storage import InvalidTokenError, NotesRepository, StorageError, build_repository

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("personal_notes_api")

DEFAULT_OWNER_ID = "default-user"
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

ERROR_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    422: "validation_error",
    503: "storage_unavailable",
}

app = FastAPI(
    title="Personal Notes API",
    version="1.0.0",
    description="REST API for creating, listing, fetching, updating and deleting personal notes.",
)

_REPOSITORY: Optional[NotesRepository] = None


def get_repository() -> NotesRepository:
    """Return the process-wide repository instance (lazily constructed)."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = build_repository()
    return _REPOSITORY


def set_repository(repository: Optional[NotesRepository]) -> None:
    """Override the process-wide repository (used by local tooling/tests)."""
    global _REPOSITORY
    _REPOSITORY = repository


def _owner_id() -> str:
    """Owner partition used for every note (single-tenant deployment)."""
    return os.environ.get("NOTES_OWNER_ID", DEFAULT_OWNER_ID)


def _now() -> str:
    """Current UTC time as an ISO-8601 string with a trailing ``Z``."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
    """Render HTTP errors using the shared ErrorResponse shape."""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    payload = {"detail": detail, "code": ERROR_CODES.get(exc.status_code, "error")}
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request, exc: RequestValidationError) -> JSONResponse:
    """Render request validation failures using the ErrorResponse shape."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        parts.append("{0}: {1}".format(location, error.get("msg", "invalid value")))
    detail = "; ".join(parts) or "Request validation failed"
    return JSONResponse(status_code=422, content={"detail": detail, "code": "validation_error"})


@app.exception_handler(StorageError)
async def storage_exception_handler(_request, exc: StorageError) -> JSONResponse:
    """Translate storage backend failures into 503 responses."""
    LOGGER.error("storage failure: %s", exc)
    payload = {"detail": "Notes storage is unavailable", "code": "storage_unavailable"}
    return JSONResponse(status_code=503, content=payload)


@app.get("/health", tags=["system"])
def health() -> Dict[str, str]:
    """Liveness/readiness probe."""
    return {"status": "ok", "service": "personal_notes_api"}


@app.post(
    "/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    tags=["notes"],
    responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def create_note(
    payload: NoteCreateRequest,
    repo: NotesRepository = Depends(get_repository),
) -> Note:
    """Create a note and return it with generated id and timestamps."""
    now = _now()
    item: Dict[str, Any] = {
        "note_id": str(uuid.uuid4()),
        "owner_id": _owner_id(),
        "title": payload.title,
        "body": payload.body,
        "created_at": now,
        "updated_at": now,
    }
    repo.put_note(item)
    LOGGER.info("note created note_id=%s owner_id=%s", item["note_id"], item["owner_id"])
    return Note(**item)


@app.get(
    "/notes",
    response_model=NoteListResponse,
    tags=["notes"],
    responses={400: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def list_notes(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    next_token: Optional[str] = Query(None),
    repo: NotesRepository = Depends(get_repository),
) -> NoteListResponse:
    """List notes for the current owner with opaque cursor pagination."""
    try:
        items, token = repo.list_notes(_owner_id(), limit, next_token)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=400, detail="Invalid next_token") from exc
    notes = [Note(**item) for item in items]
    return NoteListResponse(items=notes, count=len(notes), next_token=token)


@app.get(
    "/notes/{note_id}",
    response_model=Note,
    tags=["notes"],
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_note(note_id: str, repo: NotesRepository = Depends(get_repository)) -> Note:
    """Fetch a single note by id."""
    item = repo.get_note(_owner_id(), note_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return Note(**item)


@app.put(
    "/notes/{note_id}",
    response_model=Note,
    tags=["notes"],
    responses={
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def update_note(
    note_id: str,
    payload: NoteUpdateRequest,
    repo: NotesRepository = Depends(get_repository),
) -> Note:
    """Replace the title and body of an existing note."""
    item = repo.update_note(_owner_id(), note_id, payload.title, payload.body, _now())
    if item is None:
        raise HTTPException(status_code=404, detail="Note not found")
    LOGGER.info("note updated note_id=%s", note_id)
    return Note(**item)


@app.delete(
    "/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["notes"],
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def delete_note(note_id: str, repo: NotesRepository = Depends(get_repository)) -> Response:
    """Delete a note by id."""
    deleted = repo.delete_note(_owner_id(), note_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Note not found")
    LOGGER.info("note deleted note_id=%s", note_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
