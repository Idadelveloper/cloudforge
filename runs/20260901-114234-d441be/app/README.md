# loyalty_points_service

Customer loyalty-points service built with FastAPI.

* **DynamoDB** – `loyalty-customers` (accounts and balances), `loyalty-transactions`
  (append-only ledger), `loyalty-idempotency` (dedupe table with TTL).
* **SQS** – `loyalty-purchases-queue` buffers accepted purchases (`loyalty-purchases-dlq`
  is the redrive target).
* **SNS** – `loyalty-gold-tier-upgrades` receives a message when a balance first
  crosses 1000 points.
* **S3** – `loyalty-audit-log` stores one JSON object per balance change under
  `audit/{customer_id}/{yyyy}/{mm}/{dd}/{transaction_id}.json`.

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/health` | Liveness probe + reachability of DynamoDB/SQS/SNS/S3 |
| POST | `/customers` | Create an account (zero balance, `standard` tier) |
| GET | `/customers/{customer_id}` | Fetch the account record |
| GET | `/customers/{customer_id}/balance` | Current balance and tier |
| POST | `/purchases` | Submit a purchase with an idempotency key (202 accepted, 200 duplicate) |
| GET | `/purchases/{idempotency_key}` | Processing status of a submitted purchase |
| GET | `/customers/{customer_id}/transactions` | Newest-first list with `limit` and `cursor` |
| POST | `/admin/process-queue?max_messages=N` | Synchronously drain and process queued purchases |

Idempotency is enforced twice: a conditional `PutItem` on the idempotency table at
the API edge, and a conditional `pending -> processing` claim in the worker, so
SQS at-least-once delivery can never double-award points.

## Running locally (LocalStack)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export LOYALTY_ENABLE_POLLER=true      # optional background SQS consumer

uvicorn app:app --host 127.0.0.1 --port 8000
```

Smoke test:

```bash
curl -s localhost:8000/health
CID=$(curl -s -XPOST localhost:8000/customers \
  -H 'content-type: application/json' \
  -d '{"email":"a@example.com","name":"Alice"}' | python -c 'import json,sys;print(json.load(sys.stdin)["customer_id"])')
curl -s -XPOST localhost:8000/purchases -H 'content-type: application/json' \
  -d "{\"idempotency_key\":\"k1\",\"customer_id\":\"$CID\",\"amount_cents\":150000}"
curl -s -XPOST 'localhost:8000/admin/process-queue?max_messages=10'
curl -s "localhost:8000/customers/$CID/balance"
curl -s "localhost:8000/customers/$CID/transactions?limit=10"
```

## Configuration

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `AWS_ENDPOINT_URL` | unset | Endpoint override for all AWS clients (LocalStack) |
| `AWS_DEFAULT_REGION` | `us-east-1` | Region for all AWS clients |
| `LOYALTY_CUSTOMERS_TABLE` | `loyalty-customers` | Accounts table |
| `LOYALTY_TRANSACTIONS_TABLE` | `loyalty-transactions` | Ledger table |
| `LOYALTY_IDEMPOTENCY_TABLE` | `loyalty-idempotency` | Dedupe table |
| `LOYALTY_QUEUE_NAME` / `LOYALTY_QUEUE_URL` | `loyalty-purchases-queue` | Purchase queue |
| `LOYALTY_TOPIC_NAME` / `LOYALTY_TOPIC_ARN` | `loyalty-gold-tier-upgrades` | Upgrade topic |
| `LOYALTY_AUDIT_BUCKET` | `loyalty-audit-log` | Audit-log bucket |
| `LOYALTY_GOLD_THRESHOLD` | `1000` | Points needed for gold tier |
| `LOYALTY_ENABLE_POLLER` | `false` | Run the in-process SQS consumer |
| `LOYALTY_API_KEY` | unset | Shared API key (`X-API-Key`); auth is off when unset |
| `LOYALTY_SECRET_NAME` | unset | Secrets Manager secret (e.g. `loyalty-service-config`) holding `api_key` |

## Tests

```bash
python -m pytest -q
```

The suite injects an in-memory fake repository through FastAPI's dependency
overrides, so no AWS, LocalStack or network access is required.
