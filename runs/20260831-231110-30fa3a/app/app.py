"""Bookmark manager API.

A FastAPI service that stores bookmarks (url, title, tags) in DynamoDB and
protects every data endpoint with a shared API key that is loaded from AWS
Secrets Manager.  ``/health`` is intentionally unauthenticated so that
orchestrators can probe the service before secrets are available.
"""

import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import (
    BookmarkRepository,
    DynamoBookmarkRepository,
    SecretsManagerApiKeyProvider,
    TokenError,
    bookmarks_table_name,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("bookmark_manager_api")

API_KEY_HEADER = "X-API-Key"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_TAGS = 20
MAX_TAG_LENGTH = 50
MAX_TITLE_LENGTH = 300
MAX_URL_LENGTH = 2048

_repository: Optional[BookmarkRepository] = None
_key_provider: Optional[SecretsManagerApiKeyProvider] = None


def get_repository() -> BookmarkRepository:
    """Return the process-wide bookmark repository (lazily created)."""
    global _repository
    if _repository is None:
        _repository = DynamoBookmarkRepository()
    return _repository


def get_api_key_provider() -> SecretsManagerApiKeyProvider:
    """Return the process-wide API key provider (lazily created)."""
    global _key_provider
    if _key_provider is None:
        _key_provider = SecretsManagerApiKeyProvider()
    return _key_provider


def _preload_enabled() -> bool:
    return os.environ.get("PRELOAD_API_KEY", "true").strip().lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Warm the API key cache at startup without ever failing the boot."""
    if _preload_enabled():
        try:
            if get_api_key_provider().get_api_key():
                LOGGER.info("API key loaded from Secrets Manager")
            else:
                LOGGER.warning("API key could not be loaded at startup; will retry on first request")
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("API key preload failed: %s", exc)
    yield


app = FastAPI(
    title="bookmark_manager_api",
    version="1.0.0",
    description="Bookmark manager protected by an API key stored in AWS Secrets Manager.",
    lifespan=lifespan,
)


class Bookmark(BaseModel):
    """A stored bookmark."""

    bookmark_id: str
    url: str
    title: str
    tags: List[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class BookmarkCreateRequest(BaseModel):
    """Payload accepted by ``POST /bookmarks``."""

    url: str
    title: str
    tags: List[str] = Field(default_factory=list)


class BookmarkListResponse(BaseModel):
    """Paginated bookmark listing."""

    items: List[Bookmark] = Field(default_factory=list)
    count: int = 0
    tag: Optional[str] = None
    next_token: Optional[str] = None


class DeleteResponse(BaseModel):
    """Result of a delete operation."""

    deleted: bool
    bookmark_id: str


class HealthResponse(BaseModel):
    """Liveness / readiness payload."""

    status: str
    dynamodb: str
    table: str


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """Render errors as ``{"detail": ..., "status_code": ...}``."""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": detail, "status_code": exc.status_code},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return a compact, JSON-safe representation of validation failures."""
    errors = []
    for error in exc.errors():
        errors.append(
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "msg": str(error.get("msg", "invalid value")),
            }
        )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Request validation failed",
            "status_code": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "errors": errors,
        },
    )


def _matches(presented: str, expected: Optional[str]) -> bool:
    if not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER),
    provider: SecretsManagerApiKeyProvider = Depends(get_api_key_provider),
) -> None:
    """Validate the ``X-API-Key`` header against the Secrets Manager value."""
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key header")
    if _matches(x_api_key, provider.get_api_key()):
        return
    # The cached value may be stale (rotation) - refresh once before rejecting.
    if _matches(x_api_key, provider.get_api_key(force_refresh=True)):
        return
    LOGGER.warning("Rejected request carrying an invalid API key")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key")


def _validate_url(raw: str) -> str:
    candidate = (raw or "").strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="url must be between 1 and {0} characters".format(MAX_URL_LENGTH),
        )
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="url must be a valid http:// or https:// URL",
        )
    return candidate


def _validate_title(raw: str) -> str:
    candidate = (raw or "").strip()
    if not candidate:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="title must not be empty")
    if len(candidate) > MAX_TITLE_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="title must be at most {0} characters".format(MAX_TITLE_LENGTH),
        )
    return candidate


def _normalise_tags(raw_tags: Optional[List[str]]) -> List[str]:
    tags: List[str] = []
    for raw in raw_tags or []:
        tag = str(raw).strip().lower()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="each tag must be at most {0} characters".format(MAX_TAG_LENGTH),
            )
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="at most {0} tags are allowed".format(MAX_TAGS),
        )
    return tags


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@app.get("/health", response_model=HealthResponse)
def health(repo: BookmarkRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Report service status and DynamoDB reachability."""
    try:
        reachable = bool(repo.health_check())
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("Health check failed: %s", exc)
        reachable = False
    return {
        "status": "ok" if reachable else "degraded",
        "dynamodb": "reachable" if reachable else "unreachable",
        "table": bookmarks_table_name(),
    }


@app.post(
    "/bookmarks",
    response_model=Bookmark,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
def create_bookmark(
    payload: BookmarkCreateRequest,
    repo: BookmarkRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Create a new bookmark."""
    now = _now_iso()
    item: Dict[str, Any] = {
        "bookmark_id": str(uuid4()),
        "url": _validate_url(payload.url),
        "title": _validate_title(payload.title),
        "tags": _normalise_tags(payload.tags),
        "created_at": now,
        "updated_at": now,
    }
    stored = repo.create(item)
    LOGGER.info("Created bookmark %s", stored.get("bookmark_id"))
    return stored


@app.get("/bookmarks", response_model=BookmarkListResponse, dependencies=[Depends(require_api_key)])
def list_bookmarks(
    tag: Optional[str] = Query(default=None, max_length=MAX_TAG_LENGTH),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    next_token: Optional[str] = Query(default=None, max_length=2048),
    repo: BookmarkRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List bookmarks, optionally filtered by a single tag."""
    normalised_tag: Optional[str] = None
    if tag is not None:
        normalised_tag = tag.strip().lower()
        if not normalised_tag:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="tag must not be blank",
            )
    try:
        if normalised_tag:
            items, token = repo.list_by_tag(normalised_tag, limit, next_token)
        else:
            items, token = repo.list_bookmarks(limit, next_token)
    except TokenError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"items": items, "count": len(items), "tag": normalised_tag, "next_token": token}


@app.get("/bookmarks/{bookmark_id}", response_model=Bookmark, dependencies=[Depends(require_api_key)])
def get_bookmark(
    bookmark_id: str,
    repo: BookmarkRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Fetch a single bookmark by id."""
    item = repo.get(bookmark_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bookmark not found")
    return item


@app.delete("/bookmarks/{bookmark_id}", response_model=DeleteResponse, dependencies=[Depends(require_api_key)])
def delete_bookmark(
    bookmark_id: str,
    repo: BookmarkRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Delete a bookmark by id."""
    removed = repo.delete(bookmark_id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bookmark not found")
    LOGGER.info("Deleted bookmark %s", bookmark_id)
    return {"deleted": True, "bookmark_id": bookmark_id}


def main() -> None:  # pragma: no cover - process entrypoint
    """Run the service with uvicorn."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
