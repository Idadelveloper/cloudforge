# Product Feedback Service

FastAPI JSON service that collects customer product feedback, stores every
submission in DynamoDB and publishes an SNS alert whenever a rating is 1 or 2
so support staff are notified.

## Endpoints

| Method | Path                       | Purpose |
|--------|----------------------------|---------|
| POST   | `/feedback`                | Submit feedback (`product_id`, `rating` 1-5, `comment`, optional `customer_email`). Publishes an SNS alert when `rating <= 2`. |
| GET    | `/feedback`                | List feedback, newest first. Query params: `product_id`, `rating`, `limit` (1-500, default 50). |
| GET    | `/feedback/{feedback_id}`  | Fetch a single feedback record (404 when unknown). |
| GET    | `/feedback/stats/average`  | Average rating, count and per-rating breakdown; optional `product_id`. |
| GET    | `/health`                  | Liveness/readiness probe. |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ENDPOINT_URL` | _unset_ | Set to e.g. `http://localhost:4566` for LocalStack. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | `us-east-1` | AWS region. |
| `FEEDBACK_TABLE` | `product-feedback` | DynamoDB table (partition key `feedback_id`). |
| `FEEDBACK_PRODUCT_INDEX` | `product_id-created_at-index` | GSI used for per-product listings (falls back to a scan). |
| `LOW_RATING_TOPIC_ARN` | _unset_ | SNS topic ARN for low-rating alerts. |
| `LOW_RATING_TOPIC_NAME` | `product-feedback-low-rating-alerts` | Topic name used to resolve the ARN when no ARN is set. |
| `HOST` / `PORT` | all interfaces / `8000` | Bind address when running `python app.py`. |
| `LOG_LEVEL` | `INFO` | Root log level. |

## Running locally

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_REGION=us-east-1
export FEEDBACK_TABLE=product-feedback
export LOW_RATING_TOPIC_NAME=product-feedback-low-rating-alerts
uvicorn app:app --host 0.0.0.0 --port 8000
```

Example submission:

```bash
curl -X POST http://localhost:8000/feedback \
  -H 'Content-Type: application/json' \
  -d '{"product_id":"widget-1","rating":2,"comment":"arrived damaged"}'
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite is fully offline: DynamoDB and SNS are replaced with in-memory fakes,
so no LocalStack instance, credentials or network access are required.
