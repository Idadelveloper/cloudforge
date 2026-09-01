"""Contact-form backend: FastAPI application entrypoint and routes.

Public visitors POST contact-form submissions; administrators authenticate with
an API key (X-Api-Key header, value loaded from Secrets Manager) to list,
retrieve and delete stored messages.
"""

import hmac
import logging
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from storage import (
    AdminKeyProvider,
    DynamoDBMessageRepository,
    MessageRepository,
    decode_cursor,
    encode_cursor,
    new_message_id,
    utc_now_iso,
)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("contact_form_backend")

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


class ApiError(Exception):
    """Application level error carrying an HTTP status and machine code."""

    def __init__(self, status_code: int, detail: str, code: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


class ErrorResponse(BaseModel):
    detail: str
    code: str


class MessageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    message: str = Field(min_length=1, max_length=5000)

    @field_validator("name", "message")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        candidate = value.strip()
        if len(candidate) > 254 or not EMAIL_RE.match(candidate):
            raise ValueError("value is not a valid email address")
        return candidate.lower()


class Message(BaseModel):
    message_id: str
    name: str
    email: str
    message: str
    created_at: str
    source_ip: Optional[str] = None


class MessageList(BaseModel):
    items: List[Message]
    count: int
    next_cursor: Optional[str] = None


class DeleteResponse(BaseModel):
    message_id: str
    deleted: bool


class HealthResponse(BaseModel):
    status: str
    table: str
    table_reachable: bool


_repository: MessageRepository = DynamoDBMessageRepository()
_key_provider = AdminKeyProvider()


def get_repository() -> MessageRepository:
    """Dependency returning the message repository (overridable in tests)."""
    return _repository


def get_key_provider() -> AdminKeyProvider:
    """Dependency returning the admin API key provider."""
    return _key_provider


app = FastAPI(
    title="contact_form_backend",
    description="Contact-form submissions stored in DynamoDB with an admin surface.",
    version="1.0.0",
)


@app.exception_handler(ApiError)
async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc.detail), "code": "http_error"},
    )


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": _format_validation_errors(exc.errors()), "code": "validation_error"},
    )


def _format_validation_errors(errors: List[Dict[str, Any]]) -> str:
    parts = []
    for error in errors:
        location = ".".join(str(item) for item in error.get("loc", []) if item != "body")
        message = error.get("msg", "invalid value")
        parts.append("{0}: {1}".format(location or "body", message))
    return "; ".join(parts) or "invalid request"


def require_admin(
    x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key"),
    provider: AdminKeyProvider = Depends(get_key_provider),
) -> None:
    """Validate the shared admin API key for administrator endpoints."""
    try:
        expected = provider.get_key()
    except Exception as exc:  # noqa: BLE001 - surface as 503, details logged
        logger.error("Unable to load admin API key: %s", exc)
        raise ApiError(503, "Admin API key is unavailable", "admin_key_unavailable")
    if not expected:
        raise ApiError(503, "Admin API key is not configured", "admin_key_unavailable")
    if not x_api_key or not hmac.compare_digest(str(x_api_key), str(expected)):
        raise ApiError(401, "Invalid or missing admin API key", "unauthorized")


@app.get("/health", response_model=HealthResponse)
def health(repository: MessageRepository = Depends(get_repository)) -> HealthResponse:
    """Liveness/readiness probe including DynamoDB table reachability."""
    info = repository.health()
    reachable = bool(info.get("reachable"))
    return HealthResponse(
        status="ok" if reachable else "degraded",
        table=str(info.get("table", "unknown")),
        table_reachable=reachable,
    )


@app.post(
    "/messages",
    response_model=Message,
    status_code=201,
    responses={422: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
)
def create_message(
    payload: MessageCreate,
    request: Request,
    repository: MessageRepository = Depends(get_repository),
) -> Message:
    """Store a public contact-form submission."""
    item: Dict[str, Any] = {
        "message_id": new_message_id(),
        "name": payload.name,
        "email": payload.email,
        "message": payload.message,
        "created_at": utc_now_iso(),
        "source_ip": request.client.host if request.client else None,
    }
    try:
        stored = repository.put_message(item)
    except Exception as exc:  # noqa: BLE001 - translate storage failures
        logger.error("Failed to store message: %s", exc)
        raise ApiError(502, "Failed to store the message", "storage_error")
    logger.info("contact message stored id=%s email=%s", stored["message_id"], stored["email"])
    return Message(**stored)


@app.get(
    "/messages",
    response_model=MessageList,
    dependencies=[Depends(require_admin)],
    responses={401: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
def list_messages(
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(default=None, max_length=2048),
    repository: MessageRepository = Depends(get_repository),
) -> MessageList:
    """List all stored contact messages (administrators only)."""
    start_key = None
    if cursor:
        try:
            start_key = decode_cursor(cursor)
        except Exception:  # noqa: BLE001 - opaque cursor, no details to leak
            raise ApiError(400, "The supplied cursor is not valid", "invalid_cursor")
    try:
        items, last_key = repository.list_messages(limit=limit, cursor=start_key)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list messages: %s", exc)
        raise ApiError(502, "Failed to list messages", "storage_error")
    return MessageList(
        items=[Message(**item) for item in items],
        count=len(items),
        next_cursor=encode_cursor(last_key) if last_key else None,
    )


@app.get(
    "/messages/{message_id}",
    response_model=Message,
    dependencies=[Depends(require_admin)],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def get_message(
    message_id: str,
    repository: MessageRepository = Depends(get_repository),
) -> Message:
    """Fetch a single stored message by id (administrators only)."""
    try:
        item = repository.get_message(message_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to read message %s: %s", message_id, exc)
        raise ApiError(502, "Failed to read the message", "storage_error")
    if item is None:
        raise ApiError(404, "Message not found", "not_found")
    return Message(**item)


@app.delete(
    "/messages/{message_id}",
    response_model=DeleteResponse,
    dependencies=[Depends(require_admin)],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def delete_message(
    message_id: str,
    repository: MessageRepository = Depends(get_repository),
) -> DeleteResponse:
    """Delete a single stored message by id (administrators only)."""
    try:
        deleted = repository.delete_message(message_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to delete message %s: %s", message_id, exc)
        raise ApiError(502, "Failed to delete the message", "storage_error")
    if not deleted:
        raise ApiError(404, "Message not found", "not_found")
    logger.info("contact message deleted id=%s", message_id)
    return DeleteResponse(message_id=message_id, deleted=True)


def main() -> None:
    """Run the service with uvicorn."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
