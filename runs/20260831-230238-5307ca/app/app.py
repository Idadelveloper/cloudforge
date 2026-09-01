"""Bookmark manager API.

A FastAPI service that stores bookmarks (url, title, tags) in DynamoDB and
protects every data endpoint with a shared API key that is read from AWS
Secrets Manager.
"""

import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from storage import ApiKeyProvider, BookmarkRepository

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("bookmark_manager_api")

API_KEY_HEADER = "X-API-Key"
MAX_TAGS = 25

app = FastAPI(
    title="Bookmark Manager API",
    version="1.0.0",
    description="Save, list and delete bookmarks stored in DynamoDB, protected by an API key.",
)

_repository = BookmarkRepository()
_api_key_provider = ApiKeyProvider()


def get_repository() -> BookmarkRepository:
    """Return the shared bookmark repository (overridable in tests)."""
    return _repository


def get_api_key_provider() -> ApiKeyProvider:
    """Return the shared API key provider (overridable in tests)."""
    return _api_key_provider


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalise_tags(raw_tags: List[str]) -> List[str]:
    normalised: List[str] = []
    for tag in raw_tags:
        if not isinstance(tag, str):
            continue
        cleaned = tag.strip().lower()
        if cleaned and cleaned not in normalised:
            normalised.append(cleaned)
    return normalised[:MAX_TAGS]


class BookmarkCreateRequest(BaseModel):
    """Payload accepted by POST /bookmarks."""

    url: str = Field(..., min_length=1, max_length=2048)
    title: str = Field(..., min_length=1, max_length=512)
    tags: List[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        candidate = value.strip()
        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must be an absolute http:// or https:// URL")
        return candidate

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("title must not be empty")
        return candidate

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: List[str]) -> List[str]:
        return _normalise_tags(value)


class Bookmark(BaseModel):
    """A stored bookmark record."""

    bookmark_id: str
    url: str
    title: str
    tags: List[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class BookmarkListResponse(BaseModel):
    """Response body for GET /bookmarks."""

    items: List[Bookmark] = Field(default_factory=list)
    count: int = 0
    tag: Optional[str] = None


class ErrorResponse(BaseModel):
    """Uniform error payload."""

    detail: str
    status_code: int


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    status: str
    service: str
    table: str


def _to_bookmark(item: Dict[str, Any]) -> Bookmark:
    return Bookmark(
        bookmark_id=str(item.get("bookmark_id", "")),
        url=str(item.get("url", "")),
        title=str(item.get("title", "")),
        tags=[str(tag) for tag in (item.get("tags") or [])],
        created_at=str(item.get("created_at", "")),
        updated_at=str(item.get("updated_at", "")),
    )


def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER),
    provider: ApiKeyProvider = Depends(get_api_key_provider),
) -> None:
    """Validate the shared API key presented in the X-API-Key header."""
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key header")

    try:
        expected = provider.get_api_key()
    except Exception:  # pragma: no cover - defensive, exercised via stub in tests
        logger.exception("Unable to load the API key from Secrets Manager")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key configuration unavailable",
        )

    if expected and hmac.compare_digest(x_api_key, expected):
        return

    # The cached value may be stale (secret rotation); refresh once before rejecting.
    try:
        refreshed = provider.get_api_key(force_refresh=True)
    except Exception:
        logger.warning("API key refresh failed")
        refreshed = None

    if refreshed and hmac.compare_digest(x_api_key, refreshed):
        return

    logger.info("Rejected request with an invalid API key")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Render HTTP errors using the shared error schema."""
    payload = ErrorResponse(detail=str(exc.detail), status_code=exc.status_code)
    return JSONResponse(status_code=exc.status_code, content=payload.model_dump())


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Render request validation problems using the shared error schema."""
    messages = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", []) if part != "body")
        messages.append("{0}: {1}".format(location or "body", error.get("msg", "invalid value")))
    payload = ErrorResponse(
        detail="; ".join(messages) or "Invalid request payload",
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=payload.model_dump())


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health(repository: BookmarkRepository = Depends(get_repository)) -> HealthResponse:
    """Unauthenticated liveness probe."""
    return HealthResponse(status="ok", service="bookmark_manager_api", table=repository.table_name)


@app.post(
    "/bookmarks",
    response_model=Bookmark,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
    tags=["bookmarks"],
)
def create_bookmark(
    payload: BookmarkCreateRequest,
    repository: BookmarkRepository = Depends(get_repository),
) -> Bookmark:
    """Create a bookmark and persist it in DynamoDB."""
    now = _utc_now()
    item: Dict[str, Any] = {
        "bookmark_id": str(uuid.uuid4()),
        "url": payload.url,
        "title": payload.title,
        "tags": list(payload.tags),
        "created_at": now,
        "updated_at": now,
    }
    if payload.tags:
        # Index-friendly projection used by the bookmarks-tag-index GSI.
        item["tag"] = payload.tags[0]

    try:
        stored = repository.create(item)
    except Exception:
        logger.exception("Failed to store bookmark")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to store bookmark")

    return _to_bookmark(stored or item)


@app.get(
    "/bookmarks",
    response_model=BookmarkListResponse,
    dependencies=[Depends(require_api_key)],
    tags=["bookmarks"],
)
def list_bookmarks(
    tag: Optional[str] = Query(default=None, max_length=64, description="Exact tag filter (case-insensitive)"),
    limit: int = Query(default=50, ge=1, le=100),
    repository: BookmarkRepository = Depends(get_repository),
) -> BookmarkListResponse:
    """List bookmarks, optionally filtered by an exact tag."""
    normalised_tag = tag.strip().lower() if tag else None
    try:
        items = repository.list_bookmarks(tag=normalised_tag, limit=limit)
    except Exception:
        logger.exception("Failed to list bookmarks")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to list bookmarks")

    bookmarks = [_to_bookmark(item) for item in items]
    return BookmarkListResponse(items=bookmarks, count=len(bookmarks), tag=normalised_tag)


@app.get(
    "/bookmarks/{bookmark_id}",
    response_model=Bookmark,
    dependencies=[Depends(require_api_key)],
    tags=["bookmarks"],
)
def get_bookmark(
    bookmark_id: str,
    repository: BookmarkRepository = Depends(get_repository),
) -> Bookmark:
    """Fetch a single bookmark by id."""
    try:
        item = repository.get(bookmark_id)
    except Exception:
        logger.exception("Failed to read bookmark %s", bookmark_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to read bookmark")

    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bookmark not found")
    return _to_bookmark(item)


@app.delete(
    "/bookmarks/{bookmark_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_api_key)],
    tags=["bookmarks"],
)
def delete_bookmark(
    bookmark_id: str,
    repository: BookmarkRepository = Depends(get_repository),
) -> Response:
    """Delete a bookmark by id."""
    try:
        deleted = repository.delete(bookmark_id)
    except Exception:
        logger.exception("Failed to delete bookmark %s", bookmark_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to delete bookmark")

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bookmark not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
