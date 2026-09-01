"""Expense tracking REST API backed by Amazon DynamoDB.

The HTTP layer is intentionally thin: request validation and aggregation live
here, while all persistence concerns are delegated to the repository defined in
``storage.py``.  The repository is injected through FastAPI's dependency system
so the API can be exercised in tests with an in-memory fake.
"""
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from storage import (
    DynamoDBExpenseRepository,
    ExpenseNotFoundError,
    ExpenseRepository,
    InvalidCursorError,
    StorageError,
)

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("expense_tracker_api")

DEFAULT_USER_ID = os.environ.get("DEFAULT_USER_ID", "default")
DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "USD")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CENTS = Decimal("0.01")

app = FastAPI(
    title="Expense Tracker API",
    version="1.0.0",
    description="Record expenses, list them by category or month and summarise monthly spend.",
)

_repository: Optional[ExpenseRepository] = None


def get_repository() -> ExpenseRepository:
    """Return the process-wide repository, creating it on first use."""
    global _repository
    if _repository is None:
        _repository = DynamoDBExpenseRepository()
    return _repository


class ExpenseCreateRequest(BaseModel):
    """Payload accepted by ``POST /expenses``."""

    amount: Decimal = Field(..., gt=0, description="Expense amount, must be greater than zero")
    category: str = Field(..., min_length=1, max_length=64)
    date: str = Field(..., description="ISO-8601 calendar date, YYYY-MM-DD")
    description: Optional[str] = Field(default=None, max_length=512)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    user_id: Optional[str] = Field(default=None, max_length=128)


class ExpenseUpdateRequest(BaseModel):
    """Payload accepted by ``PUT /expenses/{expense_id}``."""

    amount: Optional[Decimal] = Field(default=None, gt=0)
    category: Optional[str] = Field(default=None, min_length=1, max_length=64)
    date: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None, max_length=512)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)


def utcnow() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def validate_date(value: Any) -> str:
    """Validate a YYYY-MM-DD date string and return its canonical form."""
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="date must be an ISO-8601 calendar date (YYYY-MM-DD)")
    return parsed.strftime("%Y-%m-%d")


def validate_month(value: Any) -> str:
    """Validate a YYYY-MM month bucket string."""
    candidate = str(value).strip() if value is not None else ""
    if not MONTH_PATTERN.match(candidate):
        raise HTTPException(status_code=400, detail="month must be formatted as YYYY-MM")
    return candidate


def normalize_amount(value: Any) -> Decimal:
    """Coerce an incoming amount to a positive two decimal place value."""
    try:
        amount = Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount must be a valid decimal number")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be greater than zero")
    return amount


def normalize_user_id(value: Optional[str]) -> str:
    """Fall back to the default partition when no user is supplied."""
    if value is None:
        return DEFAULT_USER_ID
    cleaned = value.strip()
    return cleaned or DEFAULT_USER_ID


def serialize_expense(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a stored item into a JSON friendly dictionary."""
    raw_amount = item.get("amount", "0")
    try:
        amount = float(Decimal(str(raw_amount)))
    except (InvalidOperation, TypeError, ValueError):
        amount = 0.0
    return {
        "expense_id": item.get("expense_id"),
        "user_id": item.get("user_id"),
        "amount": amount,
        "currency": item.get("currency", DEFAULT_CURRENCY),
        "category": item.get("category"),
        "date": item.get("date"),
        "month": item.get("month"),
        "description": item.get("description"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def build_summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate expense items into per-category totals."""
    totals: Dict[str, Dict[str, Any]] = {}
    grand_total = Decimal("0")
    currency = DEFAULT_CURRENCY
    for item in items:
        category = str(item.get("category") or "uncategorized")
        try:
            amount = Decimal(str(item.get("amount", "0")))
        except (InvalidOperation, TypeError, ValueError):
            amount = Decimal("0")
        currency = str(item.get("currency") or currency)
        entry = totals.setdefault(category, {"total": Decimal("0"), "expense_count": 0})
        entry["total"] = entry["total"] + amount
        entry["expense_count"] = entry["expense_count"] + 1
        grand_total = grand_total + amount
    rows = [
        {
            "category": name,
            "total": float(data["total"].quantize(CENTS, rounding=ROUND_HALF_UP)),
            "expense_count": data["expense_count"],
        }
        for name, data in totals.items()
    ]
    rows.sort(key=lambda row: (-row["total"], row["category"]))
    return {
        "currency": currency,
        "totals_by_category": rows,
        "grand_total": float(grand_total.quantize(CENTS, rounding=ROUND_HALF_UP)),
        "expense_count": len(items),
    }


def _guard(func, *args, **kwargs):
    """Run a repository call, translating storage errors into HTTP errors."""
    try:
        return func(*args, **kwargs)
    except ExpenseNotFoundError:
        raise HTTPException(status_code=404, detail="expense not found")
    except InvalidCursorError:
        raise HTTPException(status_code=400, detail="cursor is not a valid pagination token")
    except StorageError as exc:
        LOGGER.error("storage failure: %s", exc)
        raise HTTPException(status_code=503, detail="storage backend unavailable")


@app.get("/health")
def health(repo: ExpenseRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Liveness probe that also reports DynamoDB reachability."""
    dependency: Dict[str, Any] = {"name": "dynamodb", "status": "ok"}
    try:
        dependency.update(repo.health())
    except Exception as exc:
        LOGGER.warning("dynamodb health check failed: %s", exc)
        dependency = {"name": "dynamodb", "status": "unavailable"}
    return {
        "status": "ok",
        "service": "expense_tracker_api",
        "dependencies": [dependency],
    }


@app.post("/expenses", status_code=201)
def create_expense(
    payload: ExpenseCreateRequest,
    repo: ExpenseRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Record a new expense."""
    category = payload.category.strip()
    if not category:
        raise HTTPException(status_code=400, detail="category must not be empty")
    date_value = validate_date(payload.date)
    amount = normalize_amount(payload.amount)
    currency = (payload.currency or DEFAULT_CURRENCY).strip().upper()
    timestamp = utcnow()
    item = {
        "expense_id": str(uuid.uuid4()),
        "user_id": normalize_user_id(payload.user_id),
        "amount": amount,
        "currency": currency,
        "category": category,
        "date": date_value,
        "month": date_value[:7],
        "description": payload.description,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _guard(repo.put, item)
    LOGGER.info("created expense %s for user %s", item["expense_id"], item["user_id"])
    return serialize_expense(item)


@app.get("/expenses")
def list_expenses(
    user_id: str = Query(default=DEFAULT_USER_ID, max_length=128),
    category: Optional[str] = Query(default=None, max_length=64),
    month: Optional[str] = Query(default=None, description="Filter by YYYY-MM"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(default=None),
    repo: ExpenseRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List expenses, optionally filtered by category and/or month."""
    owner = normalize_user_id(user_id)
    category_filter = category.strip() if category else None
    month_filter = validate_month(month) if month else None
    items, next_cursor = _guard(
        repo.list_expenses,
        owner,
        category=category_filter,
        month=month_filter,
        limit=limit,
        cursor=cursor,
    )
    serialized = [serialize_expense(item) for item in items]
    return {
        "items": serialized,
        "count": len(serialized),
        "next_cursor": next_cursor,
        "filters": {
            "user_id": owner,
            "category": category_filter,
            "month": month_filter,
            "limit": limit,
        },
    }


@app.get("/expenses/summary")
def monthly_summary(
    month: str = Query(..., description="Month bucket formatted as YYYY-MM"),
    user_id: str = Query(default=DEFAULT_USER_ID, max_length=128),
    repo: ExpenseRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Return total spend per category for the requested month."""
    owner = normalize_user_id(user_id)
    month_value = validate_month(month)
    items = _guard(repo.iter_month, owner, month_value)
    summary = build_summary(items)
    summary["month"] = month_value
    summary["user_id"] = owner
    return summary


@app.get("/expenses/{expense_id}")
def get_expense(
    expense_id: str,
    user_id: str = Query(default=DEFAULT_USER_ID, max_length=128),
    repo: ExpenseRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Fetch a single expense by identifier."""
    owner = normalize_user_id(user_id)
    item = _guard(repo.get, owner, expense_id)
    if not item:
        raise HTTPException(status_code=404, detail="expense not found")
    return serialize_expense(item)


@app.put("/expenses/{expense_id}")
def update_expense(
    expense_id: str,
    payload: ExpenseUpdateRequest,
    user_id: str = Query(default=DEFAULT_USER_ID, max_length=128),
    repo: ExpenseRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Update mutable fields of an existing expense."""
    owner = normalize_user_id(user_id)
    existing = _guard(repo.get, owner, expense_id)
    if not existing:
        raise HTTPException(status_code=404, detail="expense not found")

    changes: Dict[str, Any] = {}
    if payload.amount is not None:
        changes["amount"] = normalize_amount(payload.amount)
    if payload.category is not None:
        category = payload.category.strip()
        if not category:
            raise HTTPException(status_code=400, detail="category must not be empty")
        changes["category"] = category
    if payload.date is not None:
        date_value = validate_date(payload.date)
        changes["date"] = date_value
        changes["month"] = date_value[:7]
    if payload.description is not None:
        changes["description"] = payload.description
    if payload.currency is not None:
        changes["currency"] = payload.currency.strip().upper()
    if not changes:
        raise HTTPException(status_code=400, detail="no updatable fields supplied")

    changes["updated_at"] = utcnow()
    updated = _guard(repo.update, owner, expense_id, changes)
    return serialize_expense(updated or {})


@app.delete("/expenses/{expense_id}")
def delete_expense(
    expense_id: str,
    user_id: str = Query(default=DEFAULT_USER_ID, max_length=128),
    repo: ExpenseRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Delete an expense record."""
    owner = normalize_user_id(user_id)
    _guard(repo.delete, owner, expense_id)
    LOGGER.info("deleted expense %s for user %s", expense_id, owner)
    return {"deleted": True, "expense_id": expense_id, "user_id": owner}
