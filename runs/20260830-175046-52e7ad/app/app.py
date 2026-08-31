"""FastAPI application exposing CRUD endpoints for personal notes.

The HTTP layer is fully decoupled from persistence: routes depend on the
abstract ``NotesRepository`` interface defined in ``storage.py``, which makes it
trivial to inject an in-memory fake in tests.
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from models import ErrorResponse, Note, NoteCreateRequest, NoteListResponse, NoteUpdateRequest
from storage import DynamoDBNotesRepository, InvalidCursorError, NotesRepository, table_name

DEFAULT_USER_ID = "default-user"

app = FastAPI(
    title="personal_notes_api",
    description="REST API for managing personal notes stored in DynamoDB.",
    version="1.0.0",
)

_repository: Optional[NotesRepository] = None


class APIError(Exception):
    """Application level error carrying an HTTP status and a machine code."""

    def __init__(self, status_code: int, detail: str, code: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


def get_repository() -> NotesRepository:
    """Return the process-wide notes repository (lazily constructed)."""
    global _repository
    if _repository is None:
        _repository = DynamoDBNotesRepository()
    return _repository


def reset_repository() -> None:
    """Drop the cached repository instance (used by tests and reloads)."""
    global _repository
    _repository = None


def resolve_user_id(x_user_id: Optional[str] = Header(default=None, alias="X-User-Id")) -> str:
    """Resolve the note owner from the optional X-User-Id header."""
    if x_user_id and x_user_id.strip():
        return x_user_id.strip()
    return os.environ.get("DEFAULT_USER_ID", DEFAULT_USER_ID)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _dump(model: Any) -> Dict[str, Any]:
    """Dump a pydantic model to a dict, skipping unset/None values."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    """Render APIError instances as the documented ErrorResponse shape."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return a stable error envelope for request validation failures."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "request validation failed", "code": "validation_error"},
    )


@app.get("/health", tags=["system"])
def health() -> Dict[str, str]:
    """Liveness/readiness probe."""
    return {"status": "ok", "service": "personal_notes_api", "table": table_name()}


@app.post(
    "/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    tags=["notes"],
)
def create_note(
    payload: NoteCreateRequest,
    user_id: str = Depends(resolve_user_id),
    repo: NotesRepository = Depends(get_repository),
) -> Note:
    """Create a new note owned by the caller."""
    now = _now_iso()
    note = {
        "user_id": user_id,
        "note_id": str(uuid.uuid4()),
        "title": payload.title,
        "body": payload.body or "",
        "created_at": now,
        "updated_at": now,
    }
    created = repo.create(note)
    return Note(**created)


@app.get(
    "/notes",
    response_model=NoteListResponse,
    tags=["notes"],
    responses={400: {"model": ErrorResponse}},
)
def list_notes(
    limit: int = Query(default=25, ge=1, le=100, description="Maximum number of notes to return."),
    cursor: Optional[str] = Query(default=None, description="Opaque pagination cursor."),
    user_id: str = Depends(resolve_user_id),
    repo: NotesRepository = Depends(get_repository),
) -> NoteListResponse:
    """List the caller's notes with optional pagination."""
    try:
        items, next_cursor = repo.list(user_id, limit, cursor)
    except InvalidCursorError as exc:
        raise APIError(status.HTTP_400_BAD_REQUEST, str(exc), "invalid_cursor") from exc
    return NoteListResponse(
        items=[Note(**item) for item in items],
        next_cursor=next_cursor,
        count=len(items),
    )


@app.get(
    "/notes/{note_id}",
    response_model=Note,
    tags=["notes"],
    responses={404: {"model": ErrorResponse}},
)
def get_note(
    note_id: str,
    user_id: str = Depends(resolve_user_id),
    repo: NotesRepository = Depends(get_repository),
) -> Note:
    """Fetch a single note by identifier."""
    note = repo.get(user_id, note_id)
    if note is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "note not found", "note_not_found")
    return Note(**note)


@app.put(
    "/notes/{note_id}",
    response_model=Note,
    tags=["notes"],
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def update_note(
    note_id: str,
    payload: NoteUpdateRequest,
    user_id: str = Depends(resolve_user_id),
    repo: NotesRepository = Depends(get_repository),
) -> Note:
    """Partially update an existing note and refresh updated_at."""
    changes: Dict[str, Any] = _dump(payload)
    if not changes:
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "at least one of 'title' or 'body' must be provided",
            "no_fields_to_update",
        )
    changes["updated_at"] = _now_iso()
    updated = repo.update(user_id, note_id, changes)
    if updated is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "note not found", "note_not_found")
    return Note(**updated)


@app.delete(
    "/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    tags=["notes"],
    responses={404: {"model": ErrorResponse}},
)
def delete_note(
    note_id: str,
    user_id: str = Depends(resolve_user_id),
    repo: NotesRepository = Depends(get_repository),
) -> Response:
    """Delete a note by identifier."""
    if not repo.delete(user_id, note_id):
        raise APIError(status.HTTP_404_NOT_FOUND, "note not found", "note_not_found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
