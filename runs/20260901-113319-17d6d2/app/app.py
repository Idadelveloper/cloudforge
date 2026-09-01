"""FastAPI entrypoint for the customer loyalty-points service.

The HTTP layer is intentionally thin: every AWS interaction goes through the
repository object returned by :func:`get_repository`, which makes the service
easy to test offline (the dependency can be overridden with a fake).
"""

import os
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse

import storage
import worker
from models import CustomerCreate, PurchaseCreate

APP_NAME = "loyalty_points_service"

app = FastAPI(
    title="Loyalty Points Service",
    description="Customer loyalty accounts, asynchronous point accrual and audit logging.",
    version="1.0.0",
)

_REPOSITORY = None


def get_repository():
    """Return the process-wide repository (lazily built so imports stay cheap)."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = storage.build_repository()
    return _REPOSITORY


@app.get("/")
def root():
    """Basic service metadata."""
    return {
        "service": APP_NAME,
        "version": "1.0.0",
        "docs": "/docs",
        "gold_tier_threshold": worker.gold_threshold(),
    }


@app.get("/health")
def health(repo=Depends(get_repository)):
    """Report reachability of DynamoDB, SQS, SNS and S3."""
    checks = repo.health()
    healthy = all(value == "ok" for value in checks.values())
    body = {
        "status": "ok" if healthy else "degraded",
        "service": APP_NAME,
        "dependencies": checks,
    }
    return JSONResponse(status_code=200, content=body)


@app.post("/customers", status_code=201)
def create_customer(payload: CustomerCreate, repo=Depends(get_repository)):
    """Create a loyalty account with a zero balance on the standard tier."""
    customer_id = (payload.customer_id or uuid.uuid4().hex).strip()
    if not customer_id:
        raise HTTPException(status_code=400, detail="customer_id must not be blank")
    created = repo.create_customer(customer_id, payload.email, payload.name)
    if created is None:
        raise HTTPException(status_code=409, detail="customer already exists")
    return created


@app.get("/customers/{customer_id}")
def get_customer(customer_id: str, repo=Depends(get_repository)):
    """Fetch a customer profile record."""
    customer = repo.get_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="customer not found")
    return customer


@app.get("/customers/{customer_id}/balance")
def get_balance(customer_id: str, repo=Depends(get_repository)):
    """Return the current point balance and loyalty tier."""
    customer = repo.get_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="customer not found")
    return {
        "customer_id": customer_id,
        "points_balance": int(customer.get("points_balance", 0) or 0),
        "tier": customer.get("tier", "standard"),
        "updated_at": customer.get("updated_at"),
    }


@app.get("/customers/{customer_id}/transactions")
def list_transactions(
    customer_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
    repo=Depends(get_repository),
):
    """List a customer's transactions, newest first."""
    if not repo.get_customer(customer_id):
        raise HTTPException(status_code=404, detail="customer not found")
    try:
        items, next_cursor = repo.list_transactions(customer_id, limit=limit, cursor=cursor)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid cursor")
    return {
        "customer_id": customer_id,
        "count": len(items),
        "items": items,
        "next_cursor": next_cursor,
    }


@app.post("/purchases", status_code=202)
def submit_purchase(
    payload: PurchaseCreate,
    response: Response,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    repo=Depends(get_repository),
):
    """Reserve the idempotency key and enqueue the purchase for accrual."""
    key = (idempotency_key or payload.idempotency_key or "").strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail="an 'Idempotency-Key' header or 'idempotency_key' body field is required",
        )
    customer = repo.get_customer(payload.customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="customer not found")

    transaction_id = storage.new_transaction_id()
    points = worker.points_for_amount(payload.amount_cents)
    reserved_payload = {
        "transaction_id": transaction_id,
        "idempotency_key": key,
        "customer_id": payload.customer_id,
        "order_id": payload.order_id,
        "amount_cents": int(payload.amount_cents),
        "points": points,
        "status": "pending",
    }

    reserved = repo.reserve_idempotency_record(key, payload.customer_id, transaction_id, reserved_payload)
    if reserved is None:
        existing = repo.get_idempotency_record(key) or {}
        response.status_code = 200
        return {
            "duplicate": True,
            "accepted": False,
            "idempotency_key": key,
            "status": existing.get("status", "unknown"),
            "customer_id": existing.get("customer_id"),
            "transaction_id": existing.get("transaction_id"),
            "result": existing.get("response_payload") or {},
        }

    now = storage.utcnow_iso()
    transaction = {
        "customer_id": payload.customer_id,
        "transaction_id": transaction_id,
        "idempotency_key": key,
        "order_id": payload.order_id,
        "purchase_amount_cents": int(payload.amount_cents),
        "currency": payload.currency,
        "points_awarded": 0,
        "balance_after": int(customer.get("points_balance", 0) or 0),
        "status": "pending",
        "created_at": now,
        "occurred_at": payload.occurred_at or now,
    }
    repo.put_transaction(transaction)

    message = {
        "transaction_id": transaction_id,
        "idempotency_key": key,
        "customer_id": payload.customer_id,
        "order_id": payload.order_id,
        "amount_cents": int(payload.amount_cents),
        "points": points,
        "enqueued_at": now,
    }
    repo.enqueue_purchase(message)

    return {
        "duplicate": False,
        "accepted": True,
        "idempotency_key": key,
        "customer_id": payload.customer_id,
        "transaction_id": transaction_id,
        "order_id": payload.order_id,
        "amount_cents": int(payload.amount_cents),
        "points": points,
        "status": "pending",
    }


@app.get("/purchases/{idempotency_key}")
def get_purchase_status(idempotency_key: str, repo=Depends(get_repository)):
    """Look up the recorded result for a given idempotency key."""
    record = repo.get_idempotency_record(idempotency_key)
    if not record:
        raise HTTPException(status_code=404, detail="idempotency key not found")
    return {
        "idempotency_key": idempotency_key,
        "status": record.get("status"),
        "customer_id": record.get("customer_id"),
        "transaction_id": record.get("transaction_id"),
        "result": record.get("response_payload") or {},
        "created_at": record.get("created_at"),
    }


@app.get("/customers/{customer_id}/audit-log")
def list_audit_log(
    customer_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    include_entries: bool = Query(default=False),
    repo=Depends(get_repository),
):
    """List the S3 audit-log objects recorded for a customer's balance changes."""
    entries = repo.list_audit_entries(customer_id, limit=limit)
    if include_entries:
        for entry in entries:
            entry["entry"] = repo.get_audit_entry(entry["key"])
    return {"customer_id": customer_id, "count": len(entries), "entries": entries}


@app.post("/internal/process-queue")
def process_queue(
    max_messages: int = Query(default=10, ge=1, le=10),
    repo=Depends(get_repository),
):
    """Fallback worker trigger: drain the purchase queue synchronously.

    In AWS the accrual is done by the SQS-triggered Lambda in ``worker.py``;
    this endpoint runs exactly the same code path and is useful for local
    runs where no Lambda is wired to the queue.
    """
    results = worker.drain_queue(repo, max_messages=max_messages)
    return {"processed": len(results), "results": results}


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
