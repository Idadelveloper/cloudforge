"""URL shortener API built with FastAPI and backed by DynamoDB.

Endpoints:
    GET    /health            liveness probe + DynamoDB reachability
    POST   /shorten           create a short code for a long URL
    GET    /{code}            307 redirect + atomic visit counter increment
    GET    /api/stats/{code}  visit statistics for a code
    GET    /api/links         paginated listing of stored mappings
    DELETE /api/links/{code}  remove a mapping
"""
import logging
import os
import re
import secrets
import string
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from storage import DynamoUrlRepository, UrlRepository, table_name

APP_NAME = "url_shortener_api"
CODE_ALPHABET = string.ascii_letters + string.digits
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
MAX_URL_LENGTH = 2048
MAX_CODE_ATTEMPTS = 8
RESERVED_CODES = {
    "health",
    "shorten",
    "api",
    "docs",
    "redoc",
    "openapi.json",
    "favicon.ico",
    "static",
    "metrics",
}

LOGGER = logging.getLogger("url_shortener")
if not LOGGER.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    LOGGER.addHandler(_handler)
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


CODE_LENGTH = max(3, min(32, _int_env("SHORT_CODE_LENGTH", 7)))

app = FastAPI(
    title="URL Shortener API",
    version="1.0.0",
    description="Shorten long URLs into base62 codes stored in DynamoDB.",
)

_repository: Optional[UrlRepository] = None


def get_repository() -> UrlRepository:
    """Return the process-wide repository instance (lazily created)."""
    global _repository
    if _repository is None:
        _repository = DynamoUrlRepository()
    return _repository


class ShortenRequest(BaseModel):
    url: str = Field(..., description="Absolute http(s) URL to shorten")
    custom_code: Optional[str] = Field(default=None, description="Optional custom code, 3-32 of [A-Za-z0-9_-]")


class ShortenResponse(BaseModel):
    code: str
    short_url: str
    long_url: str
    created_at: str


class StatsResponse(BaseModel):
    code: str
    long_url: str
    visit_count: int
    created_at: str
    last_visited_at: Optional[str] = None


class LinkSummary(BaseModel):
    code: str
    long_url: str
    visit_count: int
    created_at: str
    last_visited_at: Optional[str] = None


class LinkListResponse(BaseModel):
    items: List[LinkSummary]
    count: int
    next: Optional[str] = None


class DeleteResponse(BaseModel):
    deleted: bool
    code: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _validate_url(raw: Optional[str]) -> str:
    url = (raw or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="url is required")
    if len(url) > MAX_URL_LENGTH:
        raise HTTPException(status_code=422, detail="url exceeds maximum length of 2048 characters")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=422, detail="url must be an absolute http(s) URL")
    return url


def _validate_custom_code(raw: str) -> str:
    code = raw.strip()
    if not CODE_PATTERN.match(code):
        raise HTTPException(status_code=422, detail="custom_code must be 3-32 characters of [A-Za-z0-9_-]")
    if code.lower() in RESERVED_CODES:
        raise HTTPException(status_code=422, detail="custom_code is reserved")
    return code


def _repo_call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return func(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:  # storage failures are surfaced as 503
        LOGGER.error("storage operation %s failed: %s", getattr(func, "__name__", "call"), exc)
        raise HTTPException(status_code=503, detail="storage backend unavailable") from exc


def _short_url(request: Request, code: str) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return "{0}/{1}".format(base, code)


def _as_summary(item: Dict[str, Any]) -> LinkSummary:
    return LinkSummary(
        code=str(item.get("code", "")),
        long_url=str(item.get("long_url", "")),
        visit_count=int(item.get("visit_count") or 0),
        created_at=str(item.get("created_at") or ""),
        last_visited_at=item.get("last_visited_at") or None,
    )


@app.get("/health")
def health(repo: UrlRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Report service status and DynamoDB table reachability."""
    payload: Dict[str, Any] = {"status": "ok", "service": APP_NAME, "table": table_name()}
    try:
        payload["table_status"] = repo.health()
    except Exception as exc:  # health must never raise
        LOGGER.warning("health check could not reach DynamoDB: %s", exc)
        payload["status"] = "degraded"
        payload["table_status"] = "unavailable"
        payload["detail"] = str(exc)
    return payload


@app.get("/")
def root() -> Dict[str, Any]:
    """Basic service description."""
    return {
        "service": APP_NAME,
        "status": "ok",
        "endpoints": ["/health", "/shorten", "/{code}", "/api/stats/{code}", "/api/links"],
    }


@app.post("/shorten", response_model=ShortenResponse, status_code=201)
def shorten(
    payload: ShortenRequest,
    request: Request,
    repo: UrlRepository = Depends(get_repository),
) -> ShortenResponse:
    """Create a short code for the supplied long URL."""
    long_url = _validate_url(payload.url)
    created_at = _utc_now()

    if payload.custom_code:
        code = _validate_custom_code(payload.custom_code)
        item = {
            "code": code,
            "long_url": long_url,
            "created_at": created_at,
            "visit_count": 0,
            "last_visited_at": None,
        }
        if not _repo_call(repo.create, item):
            raise HTTPException(status_code=409, detail="code already exists")
    else:
        code = ""
        for _ in range(MAX_CODE_ATTEMPTS):
            candidate = _generate_code()
            if candidate.lower() in RESERVED_CODES:
                continue
            item = {
                "code": candidate,
                "long_url": long_url,
                "created_at": created_at,
                "visit_count": 0,
                "last_visited_at": None,
            }
            if _repo_call(repo.create, item):
                code = candidate
                break
        if not code:
            raise HTTPException(status_code=503, detail="could not allocate a unique short code")

    LOGGER.info("created short code %s for %s", code, long_url)
    return ShortenResponse(
        code=code,
        short_url=_short_url(request, code),
        long_url=long_url,
        created_at=created_at,
    )


@app.get("/api/stats/{code}", response_model=StatsResponse)
def stats(code: str, repo: UrlRepository = Depends(get_repository)) -> StatsResponse:
    """Return visit statistics for a short code."""
    if not CODE_PATTERN.match(code or ""):
        raise HTTPException(status_code=404, detail="code not found")
    item = _repo_call(repo.get, code)
    if not item:
        raise HTTPException(status_code=404, detail="code not found")
    return StatsResponse(
        code=str(item.get("code", code)),
        long_url=str(item.get("long_url", "")),
        visit_count=int(item.get("visit_count") or 0),
        created_at=str(item.get("created_at") or ""),
        last_visited_at=item.get("last_visited_at") or None,
    )


@app.get("/api/links", response_model=LinkListResponse)
def list_links(
    limit: int = Query(default=25, ge=1, le=100),
    start: Optional[str] = Query(default=None, description="code to resume the scan after"),
    repo: UrlRepository = Depends(get_repository),
) -> LinkListResponse:
    """List stored mappings (paginated DynamoDB scan)."""
    items, next_key = _repo_call(repo.list_items, limit, start)
    summaries = [_as_summary(item) for item in items]
    return LinkListResponse(items=summaries, count=len(summaries), next=next_key)


@app.delete("/api/links/{code}", response_model=DeleteResponse)
def delete_link(code: str, repo: UrlRepository = Depends(get_repository)) -> DeleteResponse:
    """Delete a mapping so the short code no longer resolves."""
    if not CODE_PATTERN.match(code or ""):
        raise HTTPException(status_code=404, detail="code not found")
    if not _repo_call(repo.delete, code):
        raise HTTPException(status_code=404, detail="code not found")
    LOGGER.info("deleted short code %s", code)
    return DeleteResponse(deleted=True, code=code)


@app.get("/{code}", response_model=None)
def follow(code: str, repo: UrlRepository = Depends(get_repository)) -> RedirectResponse:
    """Increment the visit counter and redirect to the original URL."""
    if not CODE_PATTERN.match(code or ""):
        raise HTTPException(status_code=404, detail="code not found")
    item = _repo_call(repo.increment_visit, code, _utc_now())
    if not item:
        LOGGER.info("redirect miss for code %s", code)
        raise HTTPException(status_code=404, detail="code not found")
    long_url = str(item.get("long_url") or "")
    if not long_url:
        raise HTTPException(status_code=500, detail="stored mapping is missing its target URL")
    LOGGER.info("redirecting %s -> %s (visits=%s)", code, long_url, item.get("visit_count"))
    return RedirectResponse(url=long_url, status_code=307)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=_int_env("PORT", 8000),
    )
