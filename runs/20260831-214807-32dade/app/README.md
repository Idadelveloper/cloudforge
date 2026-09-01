# Shop Inventory API

A small FastAPI service that manages a shop's product inventory in DynamoDB.

## Endpoints

| Method | Path                    | Description                                              |
|--------|-------------------------|----------------------------------------------------------|
| GET    | `/health`               | Liveness probe + DynamoDB reachability                   |
| POST   | `/products`             | Create a product (`409` when the SKU exists)             |
| GET    | `/products`             | List products (`limit`, `next_token` pagination)         |
| GET    | `/products/{sku}`       | Fetch one product (`404` when unknown)                   |
| PATCH  | `/products/{sku}/stock` | Atomic signed stock delta (`409` if it would go negative)|

## Configuration

| Variable                 | Default                            | Purpose                                  |
|--------------------------|------------------------------------|------------------------------------------|
| `PRODUCTS_TABLE`         | `products`                         | DynamoDB table name (partition key `sku`)|
| `AWS_ENDPOINT_URL`       | _unset_ (real AWS)                 | Point at LocalStack, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION`     | `us-east-1`                        | AWS region                               |
| `APPLICATION_LOG_GROUP`  | `/shop-inventory-api/application`  | CloudWatch log group for structured logs |
| `HOST` / `PORT`          | `0.0.0.0` / `8000`                 | Bind address for the built-in runner     |
| `LOG_LEVEL`              | `INFO`                             | Logging verbosity                        |

## Run locally (against LocalStack)

```bash
python -m pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export PRODUCTS_TABLE=products
python app.py                       # or: uvicorn app:app --host 0.0.0.0 --port 8000
```

Interactive docs are served at `http://localhost:8000/docs`.

## Example calls

```bash
curl -X POST localhost:8000/products \
  -H 'content-type: application/json' \
  -d '{"sku":"SKU-1","name":"Blue Widget","price":9.99,"quantity":10}'

curl localhost:8000/products?limit=25
curl localhost:8000/products/SKU-1

curl -X PATCH localhost:8000/products/SKU-1/stock \
  -H 'content-type: application/json' \
  -d '{"delta":-3,"reason":"counter sale"}'
```

## Tests

```bash
python -m pytest
```

The suite injects an in-memory fake DynamoDB table, so it runs completely offline
(no AWS credentials, no LocalStack, no network).
