# Order Processing Service

FastAPI backend that accepts customer orders, stores them in DynamoDB, hands
fulfilment off asynchronously via SQS, and publishes order-status-changed events
to SNS. An SQS-triggered Lambda (`worker.handler`) performs the cloud-side
fulfilment step.

## Layout

| File | Purpose |
| --- | --- |
| `app.py` | FastAPI application and HTTP routes |
| `models.py` | Pydantic request models |
| `storage.py` | DynamoDB / SQS / SNS access behind small interfaces (plus in-memory fakes) |
| `worker.py` | SQS-triggered fulfilment Lambda handler |
| `tests/test_app.py` | Offline test-suite (no AWS or network required) |

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Liveness plus DynamoDB/SQS/SNS reachability |
| POST | `/orders` | Create an order (DynamoDB write + SQS fulfilment message) |
| GET | `/orders/{order_id}` | Full order record |
| GET | `/orders/{order_id}/status` | Lightweight status lookup |
| PATCH | `/orders/{order_id}/status` | Update status and publish an SNS notification |
| GET | `/orders?customer_id=...` | List orders by customer (GSI query, `status`, `limit`, `next_token`) |
| GET | `/customers/{customer_id}/orders` | Path alias for the customer listing |

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AWS_ENDPOINT_URL` | *(unset)* | Set to e.g. `http://localhost:4566` for LocalStack |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `ORDERS_TABLE_NAME` | `orders` | DynamoDB table |
| `ORDERS_CUSTOMER_INDEX` | `customer_id-created_at-index` | Customer GSI |
| `ORDER_QUEUE_NAME` | `order-fulfillment-queue` | SQS queue name (or set `ORDER_QUEUE_URL`) |
| `ORDER_STATUS_TOPIC_NAME` | `order-status-changed-topic` | SNS topic name (or set `ORDER_STATUS_TOPIC_ARN`) |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address for `python app.py` |
| `LOG_LEVEL` | `INFO` | Application log level |

## Run

```bash
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test

python app.py                     # or: uvicorn app:app --port 8000
```

### Example

```bash
curl -sX POST localhost:8000/orders -H 'content-type: application/json' -d '{
  "customer_id": "cust-1",
  "items": [{"sku": "SKU-1", "quantity": 2, "unit_price": 9.99}]
}'

curl -s localhost:8000/orders/<order_id>/status
curl -sX PATCH localhost:8000/orders/<order_id>/status \
  -H 'content-type: application/json' -d '{"status": "FULFILLED"}'
curl -s 'localhost:8000/orders?customer_id=cust-1&limit=10'
```

## Tests

```bash
python -m pytest -q
```

The suite injects the in-memory repository/queue/notifier implementations and
fake boto3 clients, so it runs completely offline.
