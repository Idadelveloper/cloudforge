# Shop Inventory API

A small FastAPI service that manages a shop's product catalogue and stock levels.
Products are stored in a single DynamoDB table (partition key `sku`) accessed with boto3.

## Endpoints

| Method | Path                          | Description                                                |
|--------|-------------------------------|------------------------------------------------------------|
| GET    | `/health`                     | Liveness probe + DynamoDB reachability                      |
| POST   | `/products`                   | Create a product (`409` on duplicate SKU)                   |
| GET    | `/products?limit=&cursor=`    | List products (Scan, page size 1-100, default 50)           |
| GET    | `/products/{sku}`             | Fetch one product (`404` if unknown)                        |
| POST   | `/products/{sku}/adjust-stock`| Atomic signed stock delta (`409` if it would go negative)   |

Errors are returned as `{"detail": "...", "code": "..."}`.

### Examples

```bash
curl -X POST localhost:8000/products \
  -H 'Content-Type: application/json' \
  -d '{"sku":"SKU-1","name":"Widget","price":9.99,"quantity":10}'

curl localhost:8000/products
curl localhost:8000/products/SKU-1

curl -X POST localhost:8000/products/SKU-1/adjust-stock \
  -H 'Content-Type: application/json' \
  -d '{"delta":-3,"reason":"sale"}'
```

## Configuration

| Variable              | Default                    | Purpose                                   |
|-----------------------|----------------------------|-------------------------------------------|
| `DYNAMODB_TABLE_NAME` | `shop-inventory-products`  | DynamoDB table name                       |
| `AWS_REGION`          | `us-east-1`                | AWS region (`AWS_DEFAULT_REGION` also ok) |
| `AWS_ENDPOINT_URL`    | unset                      | Set to `http://localhost:4566` for LocalStack |
| `HOST` / `PORT`       | `0.0.0.0` / `8000`         | Bind address when running `python app.py` |
| `LOG_LEVEL`           | `INFO`                     | Logging level                             |

## Running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566   # LocalStack
export AWS_REGION=us-east-1
export DYNAMODB_TABLE_NAME=shop-inventory-products

uvicorn app:app --host 0.0.0.0 --port 8000
```

Interactive docs: <http://localhost:8000/docs>

## Tests

Tests are fully offline — the API tests use an injected in-memory repository and the
storage tests drive a stubbed boto3 table, so no AWS or LocalStack instance is needed.

```bash
pip install -r requirements-dev.txt
pytest -q
flake8 --max-line-length 120 .
bandit -r . -x ./tests -ll
```
