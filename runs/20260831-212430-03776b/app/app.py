"""Contact-form backend API.

Public visitors submit contact messages; administrators (authenticated with the
``X-Admin-API-Key`` header) can list, retrieve and delete stored messages.
Messages are persisted in DynamoDB.
"""

import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import (
    DEFAULT_SECRET_NAME,
    DEFAULT_TABLE_NAME,
    DynamoDBMessageRepository,
    InvalidTokenError,
    MessageRepository,
    StorageError,
    secretsmanager_client,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("contact_form_backend")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

APP_TITLE = "contact_form_backend"
APP_VERSION = "1.0.0"

app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    description="Contact-form submissions API backed by DynamoDB.",
)

_REPOSITORY: Optional[MessageRepository] = None
_ADMIN_KEY_CACHE: Optional[str] = None


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class ContactMessageCreate(BaseModel):
    """Payload accepted from the public contact form."""

    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3, max_length=254)
    message: str = Field(..., min_length=1, max_length=5000)


class ContactMessage(BaseModel):
    """A stored contact message."""

    message_id: str
    name: str
    email: str
    message: str
    created_at: str
    source_ip: Optional[str] = None


class ContactMessageList(BaseModel):
    """Paginated list of stored contact messages."""

    items: List[ContactMessage] = []
    count: int = 0
    next_token: Optional[str] = None


class DeleteResponse(BaseModel):
    """Result of a delete operation."""

    message_id: str
    deleted: bool = True


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str
    code: str


class HealthResponse(BaseModel):
    """Liveness/readiness payload."""

    status: str
    service: str = APP_TITLE
    version: str = APP_VERSION
    table: str
    dynamodb: str


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def table_name() -> str:
    """Return the configured DynamoDB table name."""
    return os.environ.get("MESSAGES_TABLE", DEFAULT_TABLE_NAME)


def get_repository() -> MessageRepository:
    """Return the process-wide message repository (lazily constructed)."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = DynamoDBMessageRepository(table_name=table_name())
    return _REPOSITORY


def _load_admin_key_from_secrets_manager() -> Optional[str]:
    """Fetch the admin API key from AWS Secrets Manager, or ``None`` on failure."""
    secret_name = os.environ.get("ADMIN_API_KEY_SECRET_NAME", DEFAULT_SECRET_NAME)
    try:
        client = secretsmanager_client()
        response = client.get_secret_value(SecretId=secret_name)
    except Exception as exc:  # broad: any AWS/config failure means "unconfigured"
        LOGGER.warning("unable to read admin api key secret %s: %s", secret_name, exc)
        return None
    raw = response.get("SecretString")
    if not raw:
        return None
    try:
        parsed: Any = json.loads(raw)
    except ValueError:
        return raw.strip() or None
    if isinstance(parsed, dict):
        for field in ("admin_api_key", "api_key", "apiKey", "value"):
            candidate = parsed.get(field)
            if isinstance(candidate, str) and candidate:
                return candidate
        return None
    if isinstance(parsed, str) and parsed:
        return parsed
    return None


def resolve_admin_api_key() -> Optional[str]:
    """Resolve the expected admin API key from the environment or Secrets Manager."""
    global _ADMIN_KEY_CACHE
    env_key = os.environ.get("ADMIN_API_KEY")
    if env_key:
        return env_key
    if _ADMIN_KEY_CACHE is None:
        _ADMIN_KEY_CACHE = _load_admin_key_from_secrets_manager()
    return _ADMIN_KEY_CACHE


def require_admin(
    x_admin_api_key: Optional[str] = Header(default=None, alias="X-Admin-API-Key"),
) -> str:
    """FastAPI dependency enforcing the admin API key header."""
    expected = resolve_admin_api_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin api key is not configured",
        )
    if not x_admin_api_key or not hmac.compare_digest(str(x_admin_api_key), str(expected)):
        LOGGER.warning("rejected admin request with missing or invalid api key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing admin api key",
        )
    return "admin"


# --------------------------------------------------------------------------- #
# Exception handlers
# --------------------------------------------------------------------------- #
@app.exception_handler(StorageError)
async def storage_error_handler(request: Request, exc: StorageError) -> JSONResponse:
    """Translate datastore failures into 503 responses."""
    LOGGER.error("datastore failure on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "datastore unavailable", "code": "storage_error"},
    )


@app.exception_handler(InvalidTokenError)
async def invalid_token_handler(request: Request, exc: InvalidTokenError) -> JSONResponse:
    """Translate bad pagination cursors into 400 responses."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc), "code": "invalid_next_token"},
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health(repo: MessageRepository = Depends(get_repository)) -> HealthResponse:
    """Report service status and DynamoDB reachability."""
    try:
        reachable = bool(repo.health())
    except StorageError as exc:
        LOGGER.warning("health check datastore error: %s", exc)
        reachable = False
    return HealthResponse(
        status="ok" if reachable else "degraded",
        table=table_name(),
        dynamodb="reachable" if reachable else "unreachable",
    )


@app.post(
    "/messages",
    response_model=ContactMessage,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"model": ErrorResponse}},
    tags=["public"],
)
def create_message(
    payload: ContactMessageCreate,
    request: Request,
    repo: MessageRepository = Depends(get_repository),
) -> ContactMessage:
    """Accept a public contact-form submission and persist it."""
    name = payload.name.strip()
    email = payload.email.strip()
    body = payload.message.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name must not be blank")
    if not body:
        raise HTTPException(status_code=422, detail="message must not be blank")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="email is not a valid address")

    item: Dict[str, Any] = {
        "message_id": str(uuid.uuid4()),
        "name": name,
        "email": email,
        "message": body,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    client_host = request.client.host if request.client else None
    if client_host:
        item["source_ip"] = client_host

    repo.put_message(item)
    LOGGER.info("stored contact message %s", item["message_id"])
    return ContactMessage(**item)


@app.get(
    "/messages",
    response_model=ContactMessageList,
    responses={401: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
    tags=["admin"],
)
def list_messages(
    limit: int = Query(default=50, ge=1, le=100),
    next_token: Optional[str] = Query(default=None),
    repo: MessageRepository = Depends(get_repository),
    _admin: str = Depends(require_admin),
) -> ContactMessageList:
    """List stored contact messages, newest first."""
    items, token = repo.list_messages(limit=limit, next_token=next_token)
    models = [ContactMessage(**item) for item in items]
    return ContactMessageList(items=models, count=len(models), next_token=token)


@app.get(
    "/messages/{message_id}",
    response_model=ContactMessage,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    tags=["admin"],
)
def get_message(
    message_id: str,
    repo: MessageRepository = Depends(get_repository),
    _admin: str = Depends(require_admin),
) -> ContactMessage:
    """Retrieve a single contact message by id."""
    item = repo.get_message(message_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    return ContactMessage(**item)


@app.delete(
    "/messages/{message_id}",
    response_model=DeleteResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    tags=["admin"],
)
def delete_message(
    message_id: str,
    repo: MessageRepository = Depends(get_repository),
    _admin: str = Depends(require_admin),
) -> DeleteResponse:
    """Delete a single contact message by id."""
    deleted = repo.delete_message(message_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    LOGGER.info("deleted contact message %s", message_id)
    return DeleteResponse(message_id=message_id, deleted=True)


def main() -> None:  # pragma: no cover - manual entrypoint
    """Run the service with uvicorn."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
