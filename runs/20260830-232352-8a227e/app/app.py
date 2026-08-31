"""URL shortener API built with FastAPI and DynamoDB.

Endpoints:
    POST   /urls              create a short code for a long URL
    GET    /urls              list stored mappings (paginated scan)
    GET    /urls/{code}/stats visit statistics for a code
    DELETE /urls/{code}       delete a mapping
    GET    /health            liveness / readiness probe
    GET    /{code}            redirect (307) and increment the visit counter
"""

import logging
import os
import re
import secrets
import string
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from storage import (
    CodeAlreadyExistsError,
    DynamoUrlRepository,
    NotFoundError,
    StorageError,
    UrlRepository,
    table_name,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("url_shortener")

CODE_ALPHABET = string.ascii_letters + string.digits
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
RESERVED_CODES = {
    "urls",
    "health",
    "docs",
    "redoc",
    "openapi.json",
    "favicon.ico",
    "static",
}
MAX_URL_LENGTH = 2048
MAX_CODE_ATTEMPTS = 6

app = FastAPI(
    title="url_shortener",
    version="1.0.0",
    description="Create short codes for long URLs, redirect visitors and track visit counts.",
)

_repository: Optional[UrlRepository] = None


def get_repository() -> UrlRepository:
    """Return the process-wide repository (lazily created DynamoDB backed)."""
    global _repository
    if _repository is None:
        _repository = DynamoUrlRepository()
    return _repository


def code_length() -> int:
    """Length of generated short codes (env configurable)."""
    try:
        value = int(os.environ.get("SHORT_CODE_LENGTH", "7"))
    except ValueError:
        value = 7
    return max(3, min(value, 32))


def base_url() -> str:
    """Public base URL used to build the returned short URL."""
    return os.environ.get("SHORT_URL_BASE_URL", "http://localhost:8000").rstrip("/")


def build_short_url(code: str) -> str:
    return "{0}/{1}".format(base_url(), code)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(code_length()))


class CreateUrlRequest(BaseModel):
    url: str
    custom_code: Optional[str] = None


class ShortUrlResponse(BaseModel):
    code: str
    long_url: str
    short_url: str
    created_at: str
    visit_count: int


class StatsResponse(BaseModel):
    code: str
    long_url: str
    visit_count: int
    created_at: str
    last_visited_at: Optional[str] = None


class UrlListResponse(BaseModel):
    items: List[ShortUrlResponse]
    count: int
    next_code: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    table: str
    table_reachable: bool


class ErrorResponse(BaseModel):
    detail: str


def validate_long_url(raw: str) -> str:
    """Validate and normalise a submitted long URL."""
    candidate = (raw or "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="url must not be empty")
    if len(candidate) > MAX_URL_LENGTH:
        raise HTTPException(
            status_code=400,
            detail="url must be at most {0} characters".format(MAX_URL_LENGTH),
        )
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must start with http:// or https://")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="url must contain a host")
    return candidate


def validate_custom_code(raw: str) -> str:
    code = (raw or "").strip()
    if not CODE_PATTERN.match(code):
        raise HTTPException(
            status_code=400,
            detail="custom_code must be 3-32 characters of [A-Za-z0-9_-]",
        )
    if code.lower() in RESERVED_CODES:
        raise HTTPException(status_code=400, detail="custom_code '{0}' is reserved".format(code))
    return code


def to_short_url_response(item: Dict[str, Any]) -> ShortUrlResponse:
    return ShortUrlResponse(
        code=item["code"],
        long_url=item["long_url"],
        short_url=build_short_url(item["code"]),
        created_at=item.get("created_at") or "",
        visit_count=int(item.get("visit_count") or 0),
    )


def to_stats_response(item: Dict[str, Any]) -> StatsResponse:
    return StatsResponse(
        code=item["code"],
        long_url=item["long_url"],
        visit_count=int(item.get("visit_count") or 0),
        created_at=item.get("created_at") or "",
        last_visited_at=item.get("last_visited_at"),
    )


@app.get("/health", response_model=HealthResponse)
def health(repo: UrlRepository = Depends(get_repository)) -> HealthResponse:
    """Report service status and DynamoDB table reachability."""
    try:
        reachable = bool(repo.healthy())
    except StorageError as exc:
        logger.warning("health check storage error: %s", exc)
        reachable = False
    return HealthResponse(status="ok", table=table_name(), table_reachable=reachable)


@app.post(
    "/urls",
    response_model=ShortUrlResponse,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def create_short_url(
    payload: CreateUrlRequest,
    repo: UrlRepository = Depends(get_repository),
) -> ShortUrlResponse:
    """Create a short code for the submitted long URL."""
    long_url = validate_long_url(payload.url)
    created_at = now_iso()

    if payload.custom_code is not None:
        code = validate_custom_code(payload.custom_code)
        try:
            item = repo.create(code, long_url, created_at)
        except CodeAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail="code '{0}' already exists".format(code)) from exc
        except StorageError as exc:
            logger.error("failed to store mapping: %s", exc)
            raise HTTPException(status_code=503, detail="storage unavailable") from exc
        logger.info("created custom short code %s", code)
        return to_short_url_response(item)

    for _ in range(MAX_CODE_ATTEMPTS):
        code = generate_code()
        try:
            item = repo.create(code, long_url, created_at)
        except CodeAlreadyExistsError:
            continue
        except StorageError as exc:
            logger.error("failed to store mapping: %s", exc)
            raise HTTPException(status_code=503, detail="storage unavailable") from exc
        logger.info("created short code %s", code)
        return to_short_url_response(item)

    raise HTTPException(status_code=503, detail="could not allocate a unique short code")


@app.get("/urls", response_model=UrlListResponse)
def list_short_urls(
    limit: int = Query(25, ge=1, le=100),
    start_after: Optional[str] = Query(None, description="Code returned as next_code by a previous call"),
    repo: UrlRepository = Depends(get_repository),
) -> UrlListResponse:
    """List stored mappings with their visit counts."""
    try:
        items, next_code = repo.list_urls(limit=limit, start_after=start_after)
    except StorageError as exc:
        logger.error("failed to list mappings: %s", exc)
        raise HTTPException(status_code=503, detail="storage unavailable") from exc
    return UrlListResponse(
        items=[to_short_url_response(item) for item in items],
        count=len(items),
        next_code=next_code,
    )


@app.get(
    "/urls/{code}/stats",
    response_model=StatsResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_stats(code: str, repo: UrlRepository = Depends(get_repository)) -> StatsResponse:
    """Return visit statistics for a short code."""
    try:
        item = repo.get(code)
    except StorageError as exc:
        logger.error("failed to read mapping %s: %s", code, exc)
        raise HTTPException(status_code=503, detail="storage unavailable") from exc
    if item is None:
        raise HTTPException(status_code=404, detail="unknown code '{0}'".format(code))
    return to_stats_response(item)


@app.delete("/urls/{code}", status_code=204, responses={404: {"model": ErrorResponse}})
def delete_short_url(code: str, repo: UrlRepository = Depends(get_repository)) -> Response:
    """Delete a short-code mapping."""
    try:
        repo.delete(code)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="unknown code '{0}'".format(code)) from exc
    except StorageError as exc:
        logger.error("failed to delete mapping %s: %s", code, exc)
        raise HTTPException(status_code=503, detail="storage unavailable") from exc
    logger.info("deleted short code %s", code)
    return Response(status_code=204)


@app.get("/{code}", responses={307: {"description": "Redirect"}, 404: {"model": ErrorResponse}})
def redirect_to_long_url(code: str, repo: UrlRepository = Depends(get_repository)) -> RedirectResponse:
    """Increment the visit counter and redirect to the original URL."""
    try:
        item = repo.register_visit(code, now_iso())
    except NotFoundError as exc:
        logger.info("redirect miss for code %s", code)
        raise HTTPException(status_code=404, detail="unknown code '{0}'".format(code)) from exc
    except StorageError as exc:
        logger.error("failed to register visit for %s: %s", code, exc)
        raise HTTPException(status_code=503, detail="storage unavailable") from exc
    return RedirectResponse(url=item["long_url"], status_code=307)


def main() -> None:  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
