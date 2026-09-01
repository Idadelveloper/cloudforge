# Shop Inventory API

A small FastAPI service that stores shop products in a single DynamoDB table
(partition key `sku`) and lets staff create products, list them, fetch one and
apply signed stock adjustments.

## Endpoints

| Method | Path                          | Purpose                                          |
|--------|-------------------------------|--------------------------------------------------|
| GET    | `/health`                     | Status + DynamoDB reachability                   |
| POST   | `/products`                   | Create a product (409 `sku_exists` on duplicate) |
| GET    | `/products?limit=&next_token=`| List products (paginated scan)                   |
| GET    | `/products/{sku}`             | Fetch one product (404 `not_found`)              |
| PATCH  | `/products/{sku}`             | Update `name` / `price`                          |
| POST   | `/products/{sku}/adjust-stock`| Apply signed `delta` (409 `insufficient_stock`)  |

Errors are returned as `{"error": "<code>", "detail": "<message>"}`.

## Configuration

| Variable            | Default                    | Meaning                                  |
|---------------------|----------------------------|------------------------------------------|
| `PRODUCTS_TABLE`    | `shop-inventory-products`  | DynamoDB table name                      |
| `AWS_ENDPOINT_URL`  | _(unset)_                  | Set to `http://localhost:4566` for LocalStack |
| `AWS_REGION`        | `us-east-1`                | AWS region                               |
| `HOST` / `PORT`     | `127.0.0.1` / `8000`       | Bind address for the dev server          |
| `LOG_LEVEL`         | `INFO`                     | Logging level                            |

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # when using LocalStack
export PRODUCTS_TABLE=shop-inventory-products
uvicorn app:app --host 127.0.0.1 --port 8000
```

Example:

```bash
curl -X POST localhost:8000/products \
  -H 'content-type: application/json' \
  -d '{"sku":"SKU-1","name":"Tea","price":3.5,"quantity":10}'

curl -X POST localhost:8000/products/SKU-1/adjust-stock \
  -H 'content-type: application/json' -d '{"delta":-2,"reason":"sale"}'
```

## Tests

```bash
pip install pytest httpx
pytest
```

The suite runs fully offline: it uses the in-memory repository and a stub
DynamoDB table, so no AWS credentials, network or LocalStack are required.
