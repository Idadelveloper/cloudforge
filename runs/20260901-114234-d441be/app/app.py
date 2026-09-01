"""FastAPI application for the customer loyalty-points service.

Routes are thin: they validate input, talk to the storage layer through a small
repository interface (see ``storage.py``) and delegate point accrual to
``service.py``.  The repository is injected via a FastAPI dependency so that
tests can substitute an in-memory fake and run completely offline.
"""
import hashlib
import hmac
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import config
import service
import storage

LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

IDEMPOTENCY_TTL_DAYS = 30
STANDARD_TIER = "standard"

_repository: Optional[storage.LoyaltyRepository] = None
_repository_lock = threading.Lock()


def get_repository() -> storage.LoyaltyRepository:
    """Return (and lazily build) the process-wide AWS repository."""
    global _repository
    if _repository is None:
        with _repository_lock:
            if _repository is None:
                _repository = storage.LoyaltyRepository()
    return _repository


def poller_enabled() -> bool:
    value = os.environ.get("LOYALTY_ENABLE_POLLER", "false").strip().lower()
    return value in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    poller = None
    if poller_enabled():
        poller = service.BackgroundPoller(get_repository)
        poller.start()
        LOGGER.info("background SQS purchase poller started")
    try:
        yield
    finally:
        if poller is not None:
            poller.stop()
            LOGGER.info("background SQS purchase poller stopped")


app = FastAPI(
    title="loyalty_points_service",
    version="1.0.0",
    description="Customer loyalty-points service backed by DynamoDB, SQS, SNS and S3.",
    lifespan=lifespan,
)


class CustomerCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    name: str = Field(..., min_length=1, max_length=200)
    customer_id: Optional[str] = Field(default=None, max_length=100)


class PurchaseRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=200)
    customer_id: str = Field(..., min_length=1, max_length=100)
    amount_cents: int = Field(..., gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    order_id: Optional[str] = Field(default=None, max_length=200)
    occurred_at: Optional[str] = Field(default=None, max_length=64)


def to_dict(model: BaseModel) -> Dict[str, Any]:
    """pydantic v1/v2 compatible model dump."""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def request_fingerprint(payload: Dict[str, Any]) -> str:
    canonical = "|".join(
        [
            str(payload.get("customer_id", "")),
            str(payload.get("amount_cents", "")),
            str(payload.get("currency", "")),
            str(payload.get("order_id") or ""),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Shared-secret auth.  Disabled when no API key is configured."""
    expected = config.get_api_key()
    if not expected:
        return
    if not x_api_key or not hmac.compare_digest(str(x_api_key), str(expected)):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


@app.get("/health")
def health(repo: storage.LoyaltyRepository = Depends(get_repository)) -> Dict[str, Any]:
    dependencies = repo.health()
    healthy = all(state == "ok" for state in dependencies.values())
    return {
        "status": "ok" if healthy else "degraded",
        "service": "loyalty_points_service",
        "dependencies": dependencies,
        "checked_at": storage.utc_now_iso(),
    }


@app.post("/customers", status_code=201, dependencies=[Depends(require_api_key)])
def create_customer(
    body: CustomerCreate,
    repo: storage.LoyaltyRepository = Depends(get_repository),
) -> Dict[str, Any]:
    payload = to_dict(body)
    customer_id = (payload.get("customer_id") or uuid.uuid4().hex).strip()
    now = storage.utc_now_iso()
    item = {
        "customer_id": customer_id,
        "email": payload["email"],
        "name": payload["name"],
        "points_balance": 0,
        "lifetime_points": 0,
        "tier": STANDARD_TIER,
        "created_at": now,
        "updated_at": now,
    }
    if not repo.create_customer(item):
        raise HTTPException(status_code=409, detail="customer already exists")
    return item


@app.get("/customers/{customer_id}")
def get_customer(
    customer_id: str,
    repo: storage.LoyaltyRepository = Depends(get_repository),
) -> Dict[str, Any]:
    customer = repo.get_customer(customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")
    return customer


@app.get("/customers/{customer_id}/balance")
def get_balance(
    customer_id: str,
    repo: storage.LoyaltyRepository = Depends(get_repository),
) -> Dict[str, Any]:
    customer = repo.get_customer(customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")
    return {
        "customer_id": customer_id,
        "points_balance": int(customer.get("points_balance", 0)),
        "lifetime_points": int(customer.get("lifetime_points", 0)),
        "tier": customer.get("tier", STANDARD_TIER),
        "updated_at": customer.get("updated_at"),
    }


@app.post("/purchases", dependencies=[Depends(require_api_key)])
def submit_purchase(
    body: PurchaseRequest,
    repo: storage.LoyaltyRepository = Depends(get_repository),
) -> JSONResponse:
    payload = to_dict(body)
    customer_id = payload["customer_id"]
    if repo.get_customer(customer_id) is None:
        raise HTTPException(status_code=404, detail="customer not found")

    key = payload["idempotency_key"]
    fingerprint = request_fingerprint(payload)
    now = storage.utc_now_iso()
    record = {
        "idempotency_key": key,
        "customer_id": customer_id,
        "status": "pending",
        "transaction_id": None,
        "points_awarded": None,
        "request_fingerprint": fingerprint,
        "created_at": now,
        "expires_at": int(time.time()) + IDEMPOTENCY_TTL_DAYS * 24 * 3600,
    }

    if not repo.reserve_idempotency(record):
        existing = repo.get_idempotency(key) or record
        if existing.get("request_fingerprint") and existing["request_fingerprint"] != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency key already used with a different payload")
        return JSONResponse(status_code=200, content={"status": "duplicate", "purchase": existing})

    message = {
        "idempotency_key": key,
        "customer_id": customer_id,
        "amount_cents": int(payload["amount_cents"]),
        "currency": payload.get("currency", "USD"),
        "order_id": payload.get("order_id"),
        "submitted_at": payload.get("occurred_at") or now,
    }
    message_id = repo.enqueue_purchase(message)
    return JSONResponse(
        status_code=202,
        content={"status": "accepted", "purchase": record, "message_id": message_id},
    )


@app.get("/purchases/{idempotency_key}")
def get_purchase(
    idempotency_key: str,
    repo: storage.LoyaltyRepository = Depends(get_repository),
) -> Dict[str, Any]:
    record = repo.get_idempotency(idempotency_key)
    if record is None:
        raise HTTPException(status_code=404, detail="purchase not found")
    return record


@app.get("/customers/{customer_id}/transactions")
def list_transactions(
    customer_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(default=None),
    repo: storage.LoyaltyRepository = Depends(get_repository),
) -> Dict[str, Any]:
    if repo.get_customer(customer_id) is None:
        raise HTTPException(status_code=404, detail="customer not found")
    try:
        items, next_cursor = repo.list_transactions(customer_id, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "customer_id": customer_id,
        "count": len(items),
        "transactions": items,
        "next_cursor": next_cursor,
    }


@app.post("/admin/process-queue", dependencies=[Depends(require_api_key)])
def process_queue(
    max_messages: int = Query(default=10, ge=1, le=100),
    repo: storage.LoyaltyRepository = Depends(get_repository),
) -> Dict[str, Any]:
    results = service.drain_queue(repo, max_messages=max_messages)
    processed = [item for item in results if item.get("status") == "processed"]
    skipped = [item for item in results if item.get("status") == "skipped"]
    return {
        "received": len(results),
        "processed": len(processed),
        "skipped": len(skipped),
        "results": results,
    }


def main() -> None:  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
