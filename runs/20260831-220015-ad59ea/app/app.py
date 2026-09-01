"""FastAPI application exposing the shop inventory REST API.

Routes:
    GET  /health                      liveness probe + DynamoDB reachability
    POST /products                    create a product
    GET  /products                    list products (paginated scan)
    GET  /products/{sku}              fetch one product
    POST /products/{sku}/adjust-stock atomic signed stock adjustment
"""

import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import storage
from models import (
    Product,
    ProductCreateRequest,
    ProductListResponse,
    StockAdjustmentRequest,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("shop_inventory_api")

DEFAULT_ERROR_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    500: "internal_error",
    503: "service_unavailable",
}

app = FastAPI(
    title="Shop Inventory API",
    version="1.0.0",
    description="Inventory management for a small shop: products and stock adjustments backed by DynamoDB.",
)

_repository: Optional[storage.ProductRepository] = None


class ApiError(HTTPException):
    """HTTPException carrying a machine readable error code."""

    def __init__(self, status_code: int, detail: str, code: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


def get_repository() -> storage.ProductRepository:
    """Return the process-wide product repository (lazily created)."""
    global _repository
    if _repository is None:
        _repository = storage.DynamoProductRepository()
    return _repository


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Render every HTTP error as {"detail": ..., "code": ...}."""
    code = getattr(exc, "code", None) or DEFAULT_ERROR_CODES.get(exc.status_code, "error")
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc.detail), "code": code},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Render pydantic validation problems in the shared error shape."""
    errors: List[Dict[str, Any]] = []
    for item in exc.errors():
        errors.append(
            {
                "loc": [str(part) for part in item.get("loc", [])],
                "msg": str(item.get("msg", "")),
                "type": str(item.get("type", "")),
            }
        )
    return JSONResponse(
        status_code=422,
        content={"detail": "Request validation failed", "code": "validation_error", "errors": errors},
    )


@app.get("/health", tags=["system"])
def health(repo: storage.ProductRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Liveness probe; also reports whether the DynamoDB table answers."""
    try:
        reachable = bool(repo.ping())
    except Exception:  # pragma: no cover - defensive, ping already swallows errors
        LOGGER.exception("health check failed")
        reachable = False
    return {
        "status": "ok",
        "service": "shop_inventory_api",
        "table": storage.table_name(),
        "dynamodb": "ok" if reachable else "unavailable",
    }


@app.post("/products", response_model=Product, status_code=201, tags=["products"])
def create_product(
    payload: ProductCreateRequest,
    repo: storage.ProductRepository = Depends(get_repository),
) -> Product:
    """Create a product; duplicate SKUs are rejected with 409."""
    price = Decimal(payload.price).quantize(Decimal("0.01"))
    try:
        item = repo.create_product(
            sku=payload.sku,
            name=payload.name,
            price=price,
            quantity=payload.quantity,
        )
    except storage.ProductAlreadyExists as exc:
        raise ApiError(409, str(exc), "product_exists") from exc
    except storage.StorageError as exc:
        LOGGER.error("create_product storage failure: %s", exc)
        raise ApiError(503, "Inventory storage is unavailable", "storage_error") from exc
    LOGGER.info("product created sku=%s quantity=%s", item["sku"], item["quantity"])
    return Product(**item)


@app.get("/products", response_model=ProductListResponse, tags=["products"])
def list_products(
    limit: int = Query(50, ge=1, le=100, description="Maximum number of products to return"),
    cursor: Optional[str] = Query(None, description="Opaque pagination cursor from a previous response"),
    repo: storage.ProductRepository = Depends(get_repository),
) -> ProductListResponse:
    """List products with an optional page size and opaque cursor."""
    try:
        items, next_cursor = repo.list_products(limit=limit, cursor=cursor)
    except storage.InvalidCursor as exc:
        raise ApiError(400, str(exc), "invalid_cursor") from exc
    except storage.StorageError as exc:
        LOGGER.error("list_products storage failure: %s", exc)
        raise ApiError(503, "Inventory storage is unavailable", "storage_error") from exc
    products = [Product(**item) for item in items]
    return ProductListResponse(items=products, next_cursor=next_cursor, count=len(products))


@app.get("/products/{sku}", response_model=Product, tags=["products"])
def get_product(
    sku: str = Path(..., min_length=1, max_length=64, description="Product SKU"),
    repo: storage.ProductRepository = Depends(get_repository),
) -> Product:
    """Fetch a single product by SKU."""
    try:
        item = repo.get_product(sku)
    except storage.StorageError as exc:
        LOGGER.error("get_product storage failure: %s", exc)
        raise ApiError(503, "Inventory storage is unavailable", "storage_error") from exc
    if item is None:
        raise ApiError(404, f"Product with sku '{sku}' was not found", "product_not_found")
    return Product(**item)


@app.post("/products/{sku}/adjust-stock", response_model=Product, tags=["products"])
def adjust_stock(
    payload: StockAdjustmentRequest,
    sku: str = Path(..., min_length=1, max_length=64, description="Product SKU"),
    repo: storage.ProductRepository = Depends(get_repository),
) -> Product:
    """Apply a signed stock delta atomically; never lets quantity go negative."""
    try:
        item = repo.adjust_stock(sku=sku, delta=payload.delta)
    except storage.ProductNotFound as exc:
        raise ApiError(404, str(exc), "product_not_found") from exc
    except storage.InsufficientStock as exc:
        raise ApiError(409, str(exc), "insufficient_stock") from exc
    except storage.StorageError as exc:
        LOGGER.error("adjust_stock storage failure: %s", exc)
        raise ApiError(503, "Inventory storage is unavailable", "storage_error") from exc
    LOGGER.info(
        "stock adjusted sku=%s delta=%s quantity=%s reason=%s",
        sku,
        payload.delta,
        item["quantity"],
        payload.reason or "-",
    )
    return Product(**item)


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),  # nosec B104 - container deployments must bind all interfaces
        port=int(os.environ.get("PORT", "8000")),
    )
