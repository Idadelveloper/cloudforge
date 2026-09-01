# Order Processing Service

FastAPI backend that accepts orders, stores them in DynamoDB, enqueues a
fulfilment message on SQS and publishes order status changes to SNS.

## Endpoints

| Method | Path                        | Purpose                                          |
|--------|-----------------------------|--------------------------------------------------|
| POST   | `/orders`                   | Create an order (DynamoDB + SQS + SNS)           |
| GET    | `/orders/{order_id}`        | Fetch a single order (404 when unknown)          |
| GET    | `/orders?customer_id=...`   | List a customer's orders (`status`, `limit`)     |
| PATCH  | `/orders/{order_id}/status` | Change status and notify SNS subscribers         |
| GET    | `/health`                   | Liveness probe + resolved AWS configuration      |

## Configuration

| Variable                  | Default                          | Meaning                              |
|---------------------------|----------------------------------|--------------------------------------|
| `AWS_ENDPOINT_URL`        | _unset_ (real AWS)               | Point at LocalStack, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION`      | `us-east-1`                      | AWS region                           |
| `ORDERS_TABLE_NAME`       | `orders`                         | DynamoDB table                       |
| `ORDERS_CUSTOMER_INDEX`   | `customer_id-created_at-index`   | GSI used for customer listings       |
| `ORDER_QUEUE_NAME`        | `order-fulfilment-queue`         | SQS queue name (URL looked up)       |
| `ORDER_QUEUE_URL`         | _unset_                          | Explicit queue URL (skips lookup)    |
| `ORDER_STATUS_TOPIC_ARN`  | _unset_                          | SNS topic for status events          |
| `HOST` / `PORT`           | `127.0.0.1` / `8000`             | Local bind address                   |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566        # LocalStack
export ORDER_STATUS_TOPIC_ARN=arn:aws:sns:us-east-1:000000000000:order-status-topic
uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs: <http://127.0.0.1:8000/docs>

Example request:

```bash
curl -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":"cust-1","items":[{"sku":"WIDGET-1","quantity":2,"unit_price":10.5}]}'
```

## Fulfilment worker

`worker.handler` is the Lambda entrypoint for the SQS event source mapping. It
reads each fulfilment message, advances the order to `FULFILLED` in DynamoDB and
publishes the status change to SNS, reporting partial batch failures.

## Tests

```bash
pip install pytest
pytest -q
```

Tests are fully offline: DynamoDB, SQS and SNS are replaced with in-memory
fakes, so no AWS account or LocalStack instance is needed.
