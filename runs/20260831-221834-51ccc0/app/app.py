"""FastAPI application exposing the shop inventory REST API.

Routes:
    GET    /health                     - liveness / readiness probe
    POST   /products                   - create a product
    GET    /products                   - list products (paginated)
    GET    /products/{sku}             - fetch a single product
    PATCH  /products/{sku}             - update name / price
    POST   /products/{sku}/adjust-stock - apply a signed stock delta
"""

import logging
import os
from typing import List, Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import (
    DynamoDBProductRepository,
    InsufficientStock,
    InvalidPaginationToken,
    ProductAlreadyExists,
    ProductNotFound,
    ProductRepository,
    utc_now_iso,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("shop_inventory_api")

app = FastAPI(
    title="Shop Inventory API",
    version="1.0.0",
    description="Inventory management for a small shop: products and stock adjustments.",
)

_repository: Optional[ProductRepository] = None


def get_repository() -> ProductRepository:
    """Return the process-wide repository, creating the DynamoDB one lazily."""
    global _repository
    if _repository is None:
        _repository = DynamoDBProductRepository()
    return _repository


class APIError(Exception):
    """Error carrying an HTTP status plus a machine readable error code."""

    def __init__(self, status_code: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error = error
        self.detail = detail


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    LOGGER.info("request failed: %s %s -> %s", request.method, request.url.path, exc.error)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "detail": exc.detail},
    )


class Product(BaseModel):
    sku: str
    name: str
    price: float
    quantity: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ProductCreateRequest(BaseModel):
    sku: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    price: float = Field(..., ge=0)
    quantity: int = Field(0, ge=0)


class ProductUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    price: Optional[float] = Field(None, ge=0)


class StockAdjustmentRequest(BaseModel):
    delta: int
    reason: Optional[str] = Field(None, max_length=500)


class ProductListResponse(BaseModel):
    items: List[Product]
    count: int
    next_token: Optional[str] = None


@app.get("/health")
def health(repo: ProductRepository = Depends(get_repository)) -> dict:
    """Report service status and whether the DynamoDB table is reachable."""
    reachable = repo.healthy()
    return {
        "status": "ok" if reachable else "degraded",
        "table": repo.table_name,
        "dynamodb": "reachable" if reachable else "unreachable",
        "time": utc_now_iso(),
    }


@app.post("/products", status_code=201, response_model=Product)
def create_product(
    payload: ProductCreateRequest,
    repo: ProductRepository = Depends(get_repository),
) -> dict:
    """Create a product; 409 when the SKU already exists."""
    sku = payload.sku.strip()
    if not sku:
        raise APIError(400, "invalid_sku", "sku must not be blank")
    now = utc_now_iso()
    item = {
        "sku": sku,
        "name": payload.name.strip(),
        "price": float(payload.price),
        "quantity": int(payload.quantity),
        "created_at": now,
        "updated_at": now,
    }
    try:
        created = repo.create(item)
    except ProductAlreadyExists as exc:
        raise APIError(409, "sku_exists", str(exc)) from exc
    LOGGER.info("created product sku=%s quantity=%s", created["sku"], created["quantity"])
    return created


@app.get("/products", response_model=ProductListResponse)
def list_products(
    limit: int = Query(50, ge=1, le=200),
    next_token: Optional[str] = Query(None),
    repo: ProductRepository = Depends(get_repository),
) -> dict:
    """List products with an opaque pagination cursor."""
    try:
        items, token = repo.list_products(limit=limit, next_token=next_token)
    except InvalidPaginationToken as exc:
        raise APIError(400, "invalid_next_token", str(exc)) from exc
    return {"items": items, "count": len(items), "next_token": token}


@app.get("/products/{sku}", response_model=Product)
def get_product(
    sku: str,
    repo: ProductRepository = Depends(get_repository),
) -> dict:
    """Fetch one product by SKU."""
    product = repo.get(sku)
    if product is None:
        raise APIError(404, "not_found", f"product with sku '{sku}' was not found")
    return product


@app.patch("/products/{sku}", response_model=Product)
def update_product(
    sku: str,
    payload: ProductUpdateRequest,
    repo: ProductRepository = Depends(get_repository),
) -> dict:
    """Update the mutable attributes (name, price) of a product."""
    if payload.name is None and payload.price is None:
        raise APIError(400, "no_fields", "at least one of 'name' or 'price' must be supplied")
    name = payload.name.strip() if payload.name is not None else None
    price = float(payload.price) if payload.price is not None else None
    try:
        updated = repo.update_attributes(sku, name=name, price=price)
    except ProductNotFound as exc:
        raise APIError(404, "not_found", str(exc)) from exc
    LOGGER.info("updated product sku=%s", sku)
    return updated


@app.post("/products/{sku}/adjust-stock")
def adjust_stock(
    sku: str,
    payload: StockAdjustmentRequest,
    repo: ProductRepository = Depends(get_repository),
) -> dict:
    """Apply a signed stock delta atomically; never lets quantity go negative."""
    if payload.delta == 0:
        raise APIError(400, "invalid_delta", "delta must be a non-zero integer")
    try:
        product = repo.adjust_stock(sku, payload.delta)
    except ProductNotFound as exc:
        raise APIError(404, "not_found", str(exc)) from exc
    except InsufficientStock as exc:
        raise APIError(409, "insufficient_stock", str(exc)) from exc
    LOGGER.info("adjusted stock sku=%s delta=%s new_quantity=%s", sku, payload.delta, product["quantity"])
    response = dict(product)
    response["applied_delta"] = payload.delta
    response["reason"] = payload.reason
    return response


def main() -> None:  # pragma: no cover - manual entrypoint
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
