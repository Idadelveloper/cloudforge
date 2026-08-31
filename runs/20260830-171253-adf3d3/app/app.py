"""FastAPI application exposing a REST API for personal notes.

Notes are persisted in DynamoDB through the repository defined in ``storage``.
The caller is identified by the optional ``X-User-Id`` header and falls back to
``default-user`` so the API stays usable without an authentication provider.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DynamoDBNotesRepository,
    InvalidPageTokenError,
    NoteNotFoundError,
    NotesRepository,
    table_name,
)

APP_NAME = "personal_notes_api"
APP_VERSION = "1.0.0"
FALLBACK_USER_ID = "default-user"

TITLE_MAX_LENGTH = 200
BODY_MAX_LENGTH = 10000

ERROR_CODES = {
    400: "bad_request",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    500: "internal_error",
}

app = FastAPI(
    title="Personal Notes API",
    description="Create, list, fetch, update and delete personal notes stored in DynamoDB.",
    version=APP_VERSION,
)

_repository: Optional[NotesRepository] = None


class NoteCreateRequest(BaseModel):
    """Payload accepted when creating a note."""

    title: str = Field(..., min_length=1, max_length=TITLE_MAX_LENGTH)
    body: str = Field(..., min_length=0, max_length=BODY_MAX_LENGTH)


class NoteUpdateRequest(BaseModel):
    """Payload accepted when partially updating a note."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=TITLE_MAX_LENGTH)
    body: Optional[str] = Field(default=None, min_length=0, max_length=BODY_MAX_LENGTH)


class Note(BaseModel):
    """A stored note."""

    user_id: str
    note_id: str
    title: str
    body: str
    created_at: str
    updated_at: str


class NoteListResponse(BaseModel):
    """A page of notes belonging to the caller."""

    items: List[Note]
    next_token: Optional[str] = None
    count: int


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str
    code: str


class HealthResponse(BaseModel):
    """Health probe body."""

    status: str
    service: str
    version: str
    table: str


def get_repository() -> NotesRepository:
    """Return the process-wide notes repository (lazily created)."""
    global _repository
    if _repository is None:
        _repository = DynamoDBNotesRepository()
    return _repository


def set_repository(repository: Optional[NotesRepository]) -> None:
    """Replace the process-wide repository (used by local runs and tests)."""
    global _repository
    _repository = repository


def get_user_id(x_user_id: Optional[str] = Header(default=None)) -> str:
    """Resolve the caller identity from the optional ``X-User-Id`` header."""
    if x_user_id and x_user_id.strip():
        return x_user_id.strip()
    return os.environ.get("DEFAULT_USER_ID", FALLBACK_USER_ID)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _dump(model: BaseModel, exclude_unset: bool = False) -> Dict[str, Any]:
    """Dump a pydantic model to a dict, supporting pydantic v1 and v2."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=exclude_unset)
    return model.dict(exclude_unset=exclude_unset)  # pragma: no cover - pydantic v1


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException) -> JSONResponse:
    """Render HTTP errors using the ErrorResponse shape."""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    code = ERROR_CODES.get(exc.status_code, "error")
    return JSONResponse(status_code=exc.status_code, content={"detail": detail, "code": code})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError) -> JSONResponse:
    """Render request validation failures using the ErrorResponse shape."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Request validation failed", "code": "validation_error"},
    )


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> Dict[str, str]:
    """Liveness/readiness probe. Does not call AWS."""
    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "table": table_name(),
    }


@app.post(
    "/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"model": ErrorResponse}},
    tags=["notes"],
)
def create_note(
    payload: NoteCreateRequest,
    user_id: str = Depends(get_user_id),
    repository: NotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Create a new note for the caller."""
    timestamp = _now_iso()
    note: Dict[str, Any] = {
        "user_id": user_id,
        "note_id": str(uuid.uuid4()),
        "title": payload.title,
        "body": payload.body,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    return repository.create_note(note)


@app.get(
    "/notes",
    response_model=NoteListResponse,
    responses={400: {"model": ErrorResponse}},
    tags=["notes"],
)
def list_notes(
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    next_token: Optional[str] = Query(default=None),
    user_id: str = Depends(get_user_id),
    repository: NotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List the caller's notes with optional pagination."""
    try:
        items, token = repository.list_notes(user_id, limit=limit, next_token=next_token)
    except InvalidPageTokenError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"items": items, "next_token": token, "count": len(items)}


@app.get(
    "/notes/{note_id}",
    response_model=Note,
    responses={404: {"model": ErrorResponse}},
    tags=["notes"],
)
def get_note(
    note_id: str,
    user_id: str = Depends(get_user_id),
    repository: NotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Fetch a single note owned by the caller."""
    try:
        return repository.get_note(user_id, note_id)
    except NoteNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@app.put(
    "/notes/{note_id}",
    response_model=Note,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    tags=["notes"],
)
def update_note(
    note_id: str,
    payload: NoteUpdateRequest,
    user_id: str = Depends(get_user_id),
    repository: NotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Partially update a note and refresh ``updated_at``."""
    supplied = _dump(payload, exclude_unset=True)
    changes = {key: value for key, value in supplied.items() if value is not None}
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of 'title' or 'body' must be supplied",
        )
    changes["updated_at"] = _now_iso()
    try:
        return repository.update_note(user_id, note_id, changes)
    except NoteNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@app.delete(
    "/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse}},
    tags=["notes"],
)
def delete_note(
    note_id: str,
    user_id: str = Depends(get_user_id),
    repository: NotesRepository = Depends(get_repository),
) -> Response:
    """Delete a note owned by the caller."""
    try:
        repository.delete_note(user_id, note_id)
    except NoteNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def main() -> None:  # pragma: no cover - manual entrypoint
    """Run the API with uvicorn for local development."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    main()
