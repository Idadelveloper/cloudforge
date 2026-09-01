"""Expense tracker API: a FastAPI service backed by DynamoDB.

Endpoints:
    GET    /health                 liveness probe (also checks DynamoDB)
    POST   /expenses               create an expense
    GET    /expenses               list expenses (filter by category / month)
    GET    /expenses/{expense_id}  fetch one expense
    PUT    /expenses/{expense_id}  update one expense
    DELETE /expenses/{expense_id}  delete one expense
    GET    /summary?month=YYYY-MM  per-category spend totals for a month

The caller identity comes from the optional ``X-User-Id`` header and defaults
to ``default``. All persistence lives behind ``storage.DynamoDBExpenseRepository``
which is injected as a FastAPI dependency, so tests can swap in a fake.
"""

import logging
import os
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from http import HTTPStatus
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import storage

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("expense_tracker_api")

DEFAULT_USER_ID = os.environ.get("DEFAULT_USER_ID", "default")
DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "USD")
MAX_DESCRIPTION_LENGTH = 280
MAX_CATEGORY_LENGTH = 64
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
CENTS = Decimal("0.01")


class ExpenseCreateRequest(BaseModel):
    """Payload accepted by POST /expenses."""

    amount: Decimal
    category: str
    date: str
    currency: Optional[str] = None
    description: Optional[str] = None


class ExpenseUpdateRequest(BaseModel):
    """Payload accepted by PUT /expenses/{expense_id}; every field optional."""

    amount: Optional[Decimal] = None
    category: Optional[str] = None
    date: Optional[str] = None
    currency: Optional[str] = None
    description: Optional[str] = None


class Expense(BaseModel):
    """A stored expense record."""

    expense_id: str
    user_id: str
    amount: Decimal
    currency: str
    category: str
    date: str
    month: str
    description: Optional[str] = None
    created_at: str
    updated_at: str


class ExpenseListResponse(BaseModel):
    items: List[Expense]
    count: int
    next_cursor: Optional[str] = None


class CategorySummary(BaseModel):
    category: str
    total: Decimal
    count: int


class MonthlySummaryResponse(BaseModel):
    month: str
    currency: str
    totals_by_category: List[CategorySummary]
    grand_total: Decimal
    expense_count: int


class DeleteResponse(BaseModel):
    deleted: bool
    expense_id: str


class HealthResponse(BaseModel):
    status: str
    table: str


_repository: Optional[storage.DynamoDBExpenseRepository] = None


def get_repository() -> storage.DynamoDBExpenseRepository:
    """Return the process-wide DynamoDB repository (lazily created)."""
    global _repository
    if _repository is None:
        _repository = storage.DynamoDBExpenseRepository()
    return _repository


def get_user_id(x_user_id: Optional[str] = Header(default=None)) -> str:
    """Resolve the caller identity from the X-User-Id header."""
    if x_user_id and x_user_id.strip():
        return x_user_id.strip()
    return DEFAULT_USER_ID


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def quantize_amount(value: Any) -> Decimal:
    """Validate and round a monetary amount to two decimal places."""
    try:
        amount = Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ArithmeticError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount must be a decimal number")
    if amount <= Decimal("0"):
        raise HTTPException(status_code=400, detail="amount must be greater than zero")
    if amount >= Decimal("1000000000"):
        raise HTTPException(status_code=400, detail="amount is too large")
    return amount


def normalise_category(value: Optional[str]) -> str:
    category = (value or "").strip().lower()
    if not category:
        raise HTTPException(status_code=400, detail="category must be a non-empty string")
    if len(category) > MAX_CATEGORY_LENGTH:
        raise HTTPException(
            status_code=400,
            detail="category must be at most %d characters" % MAX_CATEGORY_LENGTH,
        )
    return category


def normalise_date(value: Optional[str]) -> str:
    raw = (value or "").strip()
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="date must be an ISO-8601 date (YYYY-MM-DD)")
    return parsed.isoformat()


def normalise_month(value: Optional[str]) -> str:
    raw = (value or "").strip()
    try:
        parsed = datetime.strptime(raw, "%Y-%m")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format")
    return parsed.strftime("%Y-%m")


def normalise_currency(value: Optional[str], fallback: str = DEFAULT_CURRENCY) -> str:
    currency = (value or "").strip().upper() or fallback
    if len(currency) != 3 or not currency.isalpha():
        raise HTTPException(status_code=400, detail="currency must be a 3-letter ISO-4217 code")
    return currency


def normalise_description(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    description = value.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise HTTPException(
            status_code=400,
            detail="description must be at most %d characters" % MAX_DESCRIPTION_LENGTH,
        )
    return description or None


def build_item(
    user_id: str,
    expense_id: str,
    amount: Decimal,
    currency: str,
    category: str,
    date_value: str,
    description: Optional[str],
    created_at: str,
    updated_at: str,
) -> Dict[str, Any]:
    """Assemble the DynamoDB item for an expense."""
    return {
        "user_id": user_id,
        "sk": "%s#%s" % (date_value, expense_id),
        "gsi1pk": "%s#%s" % (user_id, category),
        "expense_id": expense_id,
        "amount": amount,
        "currency": currency,
        "category": category,
        "date": date_value,
        "month": date_value[:7],
        "description": description,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def to_expense(item: Dict[str, Any]) -> Expense:
    """Map a stored DynamoDB item onto the API response model."""
    date_value = str(item.get("date", ""))
    return Expense(
        expense_id=str(item.get("expense_id", "")),
        user_id=str(item.get("user_id", "")),
        amount=Decimal(str(item.get("amount", "0"))),
        currency=str(item.get("currency") or DEFAULT_CURRENCY),
        category=str(item.get("category", "")),
        date=date_value,
        month=str(item.get("month") or date_value[:7]),
        description=item.get("description"),
        created_at=str(item.get("created_at", "")),
        updated_at=str(item.get("updated_at", "")),
    )


app = FastAPI(
    title="expense_tracker_api",
    version="1.0.0",
    description="Personal expense tracking API storing records in DynamoDB.",
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Render errors using the shared {error, detail} envelope."""
    try:
        phrase = HTTPStatus(exc.status_code).phrase
    except ValueError:
        phrase = "Error"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": phrase.lower().replace(" ", "_"), "detail": str(exc.detail)},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "detail": str(exc.errors())},
    )


@app.get("/health", response_model=HealthResponse)
def health(repo: Any = Depends(get_repository)) -> HealthResponse:
    """Report service health and DynamoDB reachability."""
    if not repo.health():
        raise HTTPException(status_code=503, detail="dynamodb table is not reachable")
    return HealthResponse(status="ok", table=storage.table_name())


@app.post("/expenses", response_model=Expense, status_code=201)
def create_expense(
    payload: ExpenseCreateRequest,
    user_id: str = Depends(get_user_id),
    repo: Any = Depends(get_repository),
) -> Expense:
    """Create a new expense record."""
    amount = quantize_amount(payload.amount)
    category = normalise_category(payload.category)
    date_value = normalise_date(payload.date)
    currency = normalise_currency(payload.currency)
    description = normalise_description(payload.description)
    timestamp = now_iso()
    item = build_item(
        user_id=user_id,
        expense_id=str(uuid4()),
        amount=amount,
        currency=currency,
        category=category,
        date_value=date_value,
        description=description,
        created_at=timestamp,
        updated_at=timestamp,
    )
    repo.put_expense(item)
    LOGGER.info("created expense %s for user %s", item["expense_id"], user_id)
    return to_expense(item)


@app.get("/expenses", response_model=ExpenseListResponse)
def list_expenses(
    category: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(default=None),
    user_id: str = Depends(get_user_id),
    repo: Any = Depends(get_repository),
) -> ExpenseListResponse:
    """List expenses newest-first, optionally filtered by category and month."""
    wanted_category = normalise_category(category) if category is not None else None
    wanted_month = normalise_month(month) if month is not None else None
    try:
        start_key = storage.decode_cursor(cursor)
    except ValueError:
        raise HTTPException(status_code=400, detail="cursor is not a valid pagination token")
    items, last_key = repo.list_expenses(
        user_id=user_id,
        category=wanted_category,
        month=wanted_month,
        limit=limit,
        cursor=start_key,
    )
    expenses = [to_expense(item) for item in items]
    return ExpenseListResponse(
        items=expenses,
        count=len(expenses),
        next_cursor=storage.encode_cursor(last_key),
    )


@app.get("/expenses/{expense_id}", response_model=Expense)
def get_expense(
    expense_id: str,
    user_id: str = Depends(get_user_id),
    repo: Any = Depends(get_repository),
) -> Expense:
    """Fetch a single expense by id."""
    item = repo.get_expense(user_id, expense_id)
    if item is None:
        raise HTTPException(status_code=404, detail="expense %s was not found" % expense_id)
    return to_expense(item)


@app.put("/expenses/{expense_id}", response_model=Expense)
def update_expense(
    expense_id: str,
    payload: ExpenseUpdateRequest,
    user_id: str = Depends(get_user_id),
    repo: Any = Depends(get_repository),
) -> Expense:
    """Update mutable fields of an existing expense."""
    existing = repo.get_expense(user_id, expense_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="expense %s was not found" % expense_id)

    amount = quantize_amount(payload.amount) if payload.amount is not None else Decimal(str(existing["amount"]))
    category = normalise_category(payload.category) if payload.category is not None else str(existing["category"])
    date_value = normalise_date(payload.date) if payload.date is not None else str(existing["date"])
    currency = normalise_currency(payload.currency, str(existing.get("currency") or DEFAULT_CURRENCY))
    if payload.description is not None:
        description = normalise_description(payload.description)
    else:
        description = existing.get("description")

    item = build_item(
        user_id=user_id,
        expense_id=expense_id,
        amount=amount,
        currency=currency,
        category=category,
        date_value=date_value,
        description=description,
        created_at=str(existing.get("created_at") or now_iso()),
        updated_at=now_iso(),
    )
    repo.put_expense(item)
    if item["sk"] != existing.get("sk"):
        repo.delete_expense(user_id, str(existing["sk"]))
    LOGGER.info("updated expense %s for user %s", expense_id, user_id)
    return to_expense(item)


@app.delete("/expenses/{expense_id}", response_model=DeleteResponse)
def delete_expense(
    expense_id: str,
    user_id: str = Depends(get_user_id),
    repo: Any = Depends(get_repository),
) -> DeleteResponse:
    """Delete an expense by id."""
    existing = repo.get_expense(user_id, expense_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="expense %s was not found" % expense_id)
    repo.delete_expense(user_id, str(existing["sk"]))
    LOGGER.info("deleted expense %s for user %s", expense_id, user_id)
    return DeleteResponse(deleted=True, expense_id=expense_id)


@app.get("/summary", response_model=MonthlySummaryResponse)
def monthly_summary(
    month: str = Query(...),
    user_id: str = Depends(get_user_id),
    repo: Any = Depends(get_repository),
) -> MonthlySummaryResponse:
    """Return total spend per category for the requested month."""
    wanted_month = normalise_month(month)
    totals: Dict[str, Dict[str, Any]] = {}
    grand_total = Decimal("0.00")
    expense_count = 0
    currency = DEFAULT_CURRENCY
    seen_currency = False

    for item in repo.iter_month_expenses(user_id, wanted_month):
        amount = Decimal(str(item.get("amount", "0"))).quantize(CENTS, rounding=ROUND_HALF_UP)
        category = str(item.get("category", "uncategorised"))
        bucket = totals.setdefault(category, {"total": Decimal("0.00"), "count": 0})
        bucket["total"] = bucket["total"] + amount
        bucket["count"] = bucket["count"] + 1
        grand_total += amount
        expense_count += 1
        if not seen_currency and item.get("currency"):
            currency = str(item["currency"])
            seen_currency = True

    ordered = sorted(totals.items(), key=lambda pair: (-pair[1]["total"], pair[0]))
    summaries = [
        CategorySummary(category=name, total=values["total"], count=values["count"])
        for name, values in ordered
    ]
    return MonthlySummaryResponse(
        month=wanted_month,
        currency=currency,
        totals_by_category=summaries,
        grand_total=grand_total,
        expense_count=expense_count,
    )


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=LOG_LEVEL.lower(),
    )
