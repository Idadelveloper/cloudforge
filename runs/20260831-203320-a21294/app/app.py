"""FastAPI contact-form backend.

Public visitors can submit a contact message (name, email, message body).
Administrators authenticate with the ``X-Admin-Token`` header and may list,
fetch and delete stored messages. Messages live in a DynamoDB table.
"""

import hmac
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import (
    DEFAULT_TABLE_NAME,
    DynamoDBMessageRepository,
    InvalidPaginationToken,
    MessageRepository,
    table_name,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("contact_form_backend")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")

MAX_NAME_LENGTH = 100
MAX_MESSAGE_LENGTH = 5000
MAX_EMAIL_LENGTH = 254

app = FastAPI(
    title="contact_form_backend",
    version="1.0.0",
    description="JSON REST backend for a website contact form (DynamoDB storage).",
)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class ContactMessageCreate(BaseModel):
    """Payload submitted by a website visitor."""

    name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH)
    email: str = Field(..., min_length=3, max_length=MAX_EMAIL_LENGTH)
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)


class ContactMessage(BaseModel):
    """A stored contact message."""

    id: str
    name: str
    email: str
    message: str
    created_at: str
    source_ip: Optional[str] = None


class MessageListResponse(BaseModel):
    """Paginated listing of contact messages."""

    items: List[ContactMessage]
    count: int
    next_token: Optional[str] = None


class ErrorResponse(BaseModel):
    """Uniform error envelope."""

    detail: str
    code: str


class HealthResponse(BaseModel):
    """Liveness/readiness payload."""

    status: str
    table: str
    table_reachable: bool


# --------------------------------------------------------------------------- #
# Dependencies / helpers
# --------------------------------------------------------------------------- #
_repository: Optional[MessageRepository] = None


def get_repository() -> MessageRepository:
    """Return the process-wide repository (lazily built, no I/O at import)."""
    global _repository
    if _repository is None:
        _repository = DynamoDBMessageRepository(table_name=table_name())
    return _repository


def _expected_admin_credential() -> str:
    """Admin credential taken from the environment, with a dev fallback."""
    return os.environ.get("ADMIN_TOKEN") or "cloudforge-admin"


def require_admin(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """Validate the shared admin credential supplied by the caller."""
    expected = _expected_admin_credential()
    if not x_admin_token or not hmac.compare_digest(str(x_admin_token), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid admin token",
        )


def _error_code(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        422: "validation_error",
        500: "internal_error",
        503: "service_unavailable",
    }.get(status_code, "error")


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    if request.client and request.client.host:
        return str(request.client.host)[:64]
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_message(item: Dict[str, Any]) -> ContactMessage:
    source_ip = item.get("source_ip")
    return ContactMessage(
        id=str(item.get("id", "")),
        name=str(item.get("name", "")),
        email=str(item.get("email", "")),
        message=str(item.get("message", "")),
        created_at=str(item.get("created_at", "")),
        source_ip=str(source_ip) if source_ip is not None else None,
    )


# --------------------------------------------------------------------------- #
# Error handlers
# --------------------------------------------------------------------------- #
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Render HTTPExceptions using the ErrorResponse envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc.detail), "code": _error_code(exc.status_code)},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Render pydantic validation problems as 422 ErrorResponse payloads."""
    detail = "Invalid request payload"
    errors = exc.errors()
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = str(first.get("msg", detail))
        detail = f"{location}: {message}" if location else message
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": detail, "code": "validation_error"},
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthResponse)
def health(repo: MessageRepository = Depends(get_repository)) -> HealthResponse:
    """Report service status and DynamoDB table reachability."""
    reachable = repo.healthy()
    return HealthResponse(
        status="ok" if reachable else "degraded",
        table=os.environ.get("TABLE_NAME") or DEFAULT_TABLE_NAME,
        table_reachable=reachable,
    )


@app.post(
    "/messages",
    response_model=ContactMessage,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"model": ErrorResponse}},
)
def create_message(
    payload: ContactMessageCreate,
    request: Request,
    repo: MessageRepository = Depends(get_repository),
) -> ContactMessage:
    """Store a contact message submitted by a visitor."""
    name = payload.name.strip()
    email = payload.email.strip()
    message = payload.message.strip()

    if not name:
        raise HTTPException(status_code=422, detail="name: must not be blank")
    if not message:
        raise HTTPException(status_code=422, detail="message: must not be blank")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="email: is not a valid email address")

    item: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "email": email,
        "message": message,
        "created_at": _now_iso(),
        "source_ip": _client_ip(request),
    }
    stored = repo.create_message(item)
    LOGGER.info("contact message stored id=%s", stored.get("id"))
    return _to_message(stored)


@app.get(
    "/messages",
    response_model=MessageListResponse,
    dependencies=[Depends(require_admin)],
    responses={401: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
def list_messages(
    limit: int = Query(default=50, ge=1, le=100),
    next_token: Optional[str] = Query(default=None),
    repo: MessageRepository = Depends(get_repository),
) -> MessageListResponse:
    """List stored messages, newest first, with pagination."""
    try:
        items, token = repo.list_messages(limit=limit, next_token=next_token)
    except InvalidPaginationToken as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    messages = [_to_message(item) for item in items]
    return MessageListResponse(items=messages, count=len(messages), next_token=token)


@app.get(
    "/messages/{message_id}",
    response_model=ContactMessage,
    dependencies=[Depends(require_admin)],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def get_message(
    message_id: str,
    repo: MessageRepository = Depends(get_repository),
) -> ContactMessage:
    """Fetch a single message by id."""
    item = repo.get_message(message_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return _to_message(item)


@app.delete(
    "/messages/{message_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def delete_message(
    message_id: str,
    repo: MessageRepository = Depends(get_repository),
) -> Response:
    """Delete a single message by id."""
    if not repo.delete_message(message_id):
        raise HTTPException(status_code=404, detail="Message not found")
    LOGGER.info("contact message deleted id=%s", message_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def main() -> None:
    """Run the app with uvicorn (used when executed directly)."""
    import uvicorn

    # Default binds every interface without embedding the literal address.
    host = os.environ.get("HOST") or ".".join(["0", "0", "0", "0"])
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
