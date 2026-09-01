"""Shop inventory REST API (FastAPI) backed by a single DynamoDB table.

Endpoints:
    GET    /health                -> liveness probe + DynamoDB reachability
    POST   /products              -> create a product
    GET    /products              -> list products (paginated scan)
    GET    /products/{sku}        -> fetch one product
    PATCH  /products/{sku}/stock  -> atomic signed stock adjustment
"""

import logging
import os
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from models import (
    ErrorResponse,
    HealthResponse,
    Product,
    ProductCreateRequest,
    ProductListResponse,
    StockAdjustmentRequest,
    to_dict,
)
from storage import (
    DynamoDBProductRepository,
    InsufficientStockError,
    InvalidPaginationTokenError,
    ProductExistsError,
    ProductNotFoundError,
    ProductRepository,
    configure_logging,
    products_table_name,
)

configure_logging()
logger = logging.getLogger("shop_inventory_api")

app = FastAPI(
    title="Shop Inventory API",
    version="1.0.0",
    description="Inventory management for a small shop: products, stock levels and stock adjustments.",
)

_repository: Optional[ProductRepository] = None

ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
}


def get_repository() -> ProductRepository:
    """Return the process-wide product repository (lazily constructed)."""
    global _repository
    if _repository is None:
        _repository = DynamoDBProductRepository()
        logger.info("initialised dynamodb repository for table %s", products_table_name())
    return _repository


def _error(status_code: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, "code": code})


@app.exception_handler(ProductNotFoundError)
async def handle_product_not_found(request: Request, exc: ProductNotFoundError) -> JSONResponse:
    logger.info("product not found: %s", exc)
    return _error(404, str(exc), "PRODUCT_NOT_FOUND")


@app.exception_handler(ProductExistsError)
async def handle_product_exists(request: Request, exc: ProductExistsError) -> JSONResponse:
    logger.info("duplicate product rejected: %s", exc)
    return _error(409, str(exc), "PRODUCT_EXISTS")


@app.exception_handler(InsufficientStockError)
async def handle_insufficient_stock(request: Request, exc: InsufficientStockError) -> JSONResponse:
    logger.info("stock adjustment rejected: %s", exc)
    return _error(409, str(exc), "INSUFFICIENT_STOCK")


@app.exception_handler(InvalidPaginationTokenError)
async def handle_invalid_token(request: Request, exc: InvalidPaginationTokenError) -> JSONResponse:
    return _error(400, str(exc), "INVALID_NEXT_TOKEN")


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health(repo: ProductRepository = Depends(get_repository)) -> dict:
    """Report service status and whether the DynamoDB table answers reads."""
    reachable = repo.healthy()
    return {
        "status": "ok" if reachable else "degraded",
        "table": products_table_name(),
        "dynamodb": "reachable" if reachable else "unreachable",
    }


@app.post(
    "/products",
    response_model=Product,
    status_code=201,
    responses=ERROR_RESPONSES,
    tags=["products"],
)
def create_product(
    payload: ProductCreateRequest,
    repo: ProductRepository = Depends(get_repository),
) -> dict:
    """Create a new product. Fails with 409 when the SKU is already taken."""
    item = repo.create_product(to_dict(payload))
    logger.info("created product sku=%s quantity=%s", item["sku"], item["quantity"])
    return item


@app.get("/products", response_model=ProductListResponse, responses=ERROR_RESPONSES, tags=["products"])
def list_products(
    limit: int = Query(50, ge=1, le=100, description="Maximum number of products per page"),
    next_token: Optional[str] = Query(None, description="Opaque cursor returned by a previous call"),
    repo: ProductRepository = Depends(get_repository),
) -> dict:
    """List products with optional cursor based pagination."""
    items, token = repo.list_products(limit=limit, next_token=next_token)
    return {"items": items, "count": len(items), "next_token": token}


@app.get("/products/{sku}", response_model=Product, responses=ERROR_RESPONSES, tags=["products"])
def get_product(sku: str, repo: ProductRepository = Depends(get_repository)) -> dict:
    """Fetch a single product by SKU."""
    item = repo.get_product(sku)
    if item is None:
        raise ProductNotFoundError(sku)
    return item


@app.patch("/products/{sku}/stock", response_model=Product, responses=ERROR_RESPONSES, tags=["products"])
def adjust_stock(
    sku: str,
    payload: StockAdjustmentRequest,
    repo: ProductRepository = Depends(get_repository),
):
    """Apply a signed stock delta atomically, refusing to go below zero."""
    if payload.delta == 0:
        return _error(400, "delta must be a non-zero integer", "INVALID_DELTA")
    item = repo.adjust_stock(sku, payload.delta)
    logger.info(
        "stock adjusted sku=%s delta=%s new_quantity=%s reason=%s",
        sku,
        payload.delta,
        item["quantity"],
        payload.reason or "-",
    )
    return item


def main() -> None:  # pragma: no cover - manual entrypoint
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")  # nosec B104 - bind address is configurable
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":  # pragma: no cover
    main()
