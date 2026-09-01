# Loyalty Points Service

FastAPI backend for a customer loyalty-points programme.

* Customer accounts, transactions and idempotency keys live in **DynamoDB**.
* Purchases are submitted with an `Idempotency-Key`, reserved with a conditional
  write and queued on **SQS** for asynchronous accrual.
* The accrual worker (`worker.py`, deployable as a Lambda) atomically increments
  the balance, appends a JSON audit entry to **S3** for every balance change and
  publishes an **SNS** notification the first time a balance crosses 1000 points
  (upgrade to gold tier).

Processing the same idempotency key twice never awards points twice: the API
refuses to re-enqueue a known key, and the worker claims the key with a
conditional `reserved -> processing` update before touching the balance.

## Layout

| File | Purpose |
| --- | --- |
| `app.py` | FastAPI application and HTTP routes |
| `models.py` | Pydantic request models |
| `storage.py` | boto3 data-access layer (DynamoDB / SQS / SNS / S3) |
| `worker.py` | Idempotent accrual worker + Lambda handler |
| `tests/test_app.py` | Offline test suite (fake repository, no AWS needed) |

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566      # LocalStack (optional)
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 127.0.0.1 --port 8000
```

Or simply `python app.py` (honours `HOST` and `PORT`).

Interactive docs: <http://127.0.0.1:8000/docs>

## Configuration

| Variable | Default |
| --- | --- |
| `AWS_ENDPOINT_URL` | unset (real AWS); set to `http://localhost:4566` for LocalStack |
| `AWS_DEFAULT_REGION` | `us-east-1` |
| `CUSTOMERS_TABLE` | `loyalty-customers` |
| `TRANSACTIONS_TABLE` | `loyalty-transactions` |
| `IDEMPOTENCY_TABLE` | `loyalty-idempotency` |
| `PURCHASES_QUEUE_NAME` | `loyalty-purchases-queue` |
| `PURCHASES_QUEUE_URL` | resolved from the queue name when unset |
| `AUDIT_BUCKET` | `loyalty-audit-log` |
| `TIER_TOPIC_NAME` | `loyalty-tier-upgrades` |
| `TIER_TOPIC_ARN` | resolved from the topic name when unset |
| `GOLD_TIER_THRESHOLD` | `1000` |
| `HOST` / `PORT` | `127.0.0.1` / `8000` |

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Dependency reachability probe |
| POST | `/customers` | Create a loyalty account |
| GET | `/customers/{customer_id}` | Fetch a customer profile |
| GET | `/customers/{customer_id}/balance` | Current balance and tier |
| GET | `/customers/{customer_id}/transactions` | Transactions, newest first (`limit`, `cursor`) |
| POST | `/purchases` | Submit a purchase (`Idempotency-Key` header); 202 first time, 200 replay |
| GET | `/purchases/{idempotency_key}` | Stored status/result for a key |
| GET | `/customers/{customer_id}/audit-log` | S3 audit entries (`include_entries=true` to inline) |
| POST | `/internal/process-queue` | Fallback trigger that drains the SQS queue using the worker code |

Example:

```bash
curl -X POST localhost:8000/customers \
  -H 'content-type: application/json' \
  -d '{"customer_id":"cust-1","email":"ada@example.com","name":"Ada"}'

curl -X POST localhost:8000/purchases \
  -H 'content-type: application/json' -H 'Idempotency-Key: order-42' \
  -d '{"customer_id":"cust-1","order_id":"order-42","amount_cents":150000}'

curl -X POST localhost:8000/internal/process-queue   # if no Lambda is wired
curl localhost:8000/customers/cust-1/balance
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite injects an in-memory fake repository through FastAPI's dependency
overrides, so no AWS account, credentials or LocalStack instance is required.
