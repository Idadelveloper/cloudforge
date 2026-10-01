"""FastAPI application exposing CRUD endpoints for personal notes.

The HTTP layer is fully decoupled from persistence: routes depend on the
``NotesRepository`` interface which is implemented by a DynamoDB backed
repository in production and can be replaced by an in-memory fake in tests.
"""

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import (
    DynamoDBNotesRepository,
    InvalidCursorError,
    NoteNotFoundError,
    NotesRepository,
    table_name,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("personal_notes_api")

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

CODE_BY_STATUS = {
    400: "bad_request",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    500: "internal_error",
}

app = FastAPI(
    title="Personal Notes API",
    version="1.0.0",
    description="Create, list, fetch, update and delete personal notes stored in DynamoDB.",
)


class Note(BaseModel):
    """A stored note."""

    note_id: str
    title: str
    body: str = ""
    created_at: str
    updated_at: str


class NoteCreateRequest(BaseModel):
    """Payload accepted when creating a note."""

    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(default="")


class NoteUpdateRequest(BaseModel):
    """Payload accepted when partially updating a note."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    body: Optional[str] = None


class NoteListResponse(BaseModel):
    """Paginated list of notes."""

    items: List[Note]
    next_cursor: Optional[str] = None
    count: int


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str
    code: str


class HealthResponse(BaseModel):
    """Liveness/readiness payload."""

    status: str
    table: str
    dynamodb: str


_repository: Optional[NotesRepository] = None


def get_repository() -> NotesRepository:
    """Return the process-wide repository, creating it lazily."""
    global _repository
    if _repository is None:
        _repository = DynamoDBNotesRepository()
    return _repository


def _dump(model: BaseModel, exclude_unset: bool = False) -> Dict[str, Any]:
    """Dump a pydantic model to a dict for both pydantic v1 and v2."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=exclude_unset)
    return model.dict(exclude_unset=exclude_unset)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Any, exc: HTTPException) -> JSONResponse:
    """Render HTTP errors using the shared ErrorResponse shape."""
    code = CODE_BY_STATUS.get(exc.status_code, "error")
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": detail, "code": code})


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health(repo: NotesRepository = Depends(get_repository)) -> Dict[str, str]:
    """Report service status and DynamoDB table reachability."""
    reachable = repo.healthy()
    return {
        "status": "ok" if reachable else "degraded",
        "table": table_name(),
        "dynamodb": "reachable" if reachable else "unreachable",
    }


@app.post(
    "/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    tags=["notes"],
)
def create_note(
    payload: NoteCreateRequest,
    repo: NotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Create a note and return the stored representation."""
    data = _dump(payload)
    item = repo.create(title=data["title"], body=data.get("body") or "")
    LOGGER.info("created note %s", item["note_id"])
    return item


@app.get(
    "/notes",
    response_model=NoteListResponse,
    responses={400: {"model": ErrorResponse}},
    tags=["notes"],
)
def list_notes(
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(default=None),
    repo: NotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List notes with an opaque pagination cursor."""
    try:
        items, next_cursor = repo.list_notes(limit=limit, cursor=cursor)
    except InvalidCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": items, "next_cursor": next_cursor, "count": len(items)}


@app.get(
    "/notes/{note_id}",
    response_model=Note,
    responses={404: {"model": ErrorResponse}},
    tags=["notes"],
)
def get_note(
    note_id: str,
    repo: NotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Fetch a single note by id."""
    try:
        return repo.get(note_id)
    except NoteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"note '{note_id}' not found") from exc


@app.put(
    "/notes/{note_id}",
    response_model=Note,
    responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
    tags=["notes"],
)
def update_note(
    note_id: str,
    payload: NoteUpdateRequest,
    repo: NotesRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Partially update a note's title and/or body."""
    fields = _dump(payload, exclude_unset=True)
    title = fields.get("title")
    body = fields.get("body")
    if title is None and body is None:
        raise HTTPException(status_code=400, detail="at least one of 'title' or 'body' must be supplied")
    try:
        item = repo.update(note_id, title=title, body=body)
    except NoteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"note '{note_id}' not found") from exc
    LOGGER.info("updated note %s", note_id)
    return item


@app.delete(
    "/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={404: {"model": ErrorResponse}},
    tags=["notes"],
)
def delete_note(
    note_id: str,
    repo: NotesRepository = Depends(get_repository),
) -> Response:
    """Delete a note by id."""
    try:
        repo.delete(note_id)
    except NoteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"note '{note_id}' not found") from exc
    LOGGER.info("deleted note %s", note_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    bind_host = os.environ.get("HOST", "0.0.0.0")  # nosec B104 - container services bind all interfaces
    bind_port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=bind_host, port=bind_port)
