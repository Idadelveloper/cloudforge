# product_feedback_service

FastAPI service for collecting product feedback (1-5 star rating plus a comment).
Records are stored in DynamoDB; any submission rated 1 or 2 is published to an SNS
topic so support staff are alerted.

## Endpoints

| Method | Path                  | Purpose                                                        |
|--------|-----------------------|----------------------------------------------------------------|
| POST   | `/feedback`           | Submit feedback; publishes SNS alert when `rating <= 2`         |
| GET    | `/feedback`           | List feedback (`product_id`, `min_rating`, `max_rating`, `limit`) |
| GET    | `/feedback/stats`     | Total count, average rating, per-star distribution              |
| GET    | `/feedback/{id}`      | Fetch one record (404 when unknown)                             |
| GET    | `/health`             | Status plus DynamoDB / SNS reachability                         |

## Configuration

| Variable                 | Default                          | Meaning                                    |
|--------------------------|----------------------------------|--------------------------------------------|
| `AWS_ENDPOINT_URL`       | (unset)                          | Endpoint override, e.g. LocalStack         |
| `AWS_DEFAULT_REGION`     | `us-east-1`                      | AWS region                                 |
| `FEEDBACK_TABLE_NAME`    | `product-feedback`               | DynamoDB table name                        |
| `FEEDBACK_PRODUCT_INDEX` | `product_id-created_at-index`    | GSI used for per-product queries           |
| `FEEDBACK_TOPIC_NAME`    | `low-rating-alerts`              | SNS topic name (used if no ARN given)      |
| `SNS_TOPIC_ARN`          | (resolved by name)               | Explicit SNS topic ARN                     |
| `DEFAULT_PRODUCT_ID`     | `general`                        | Used when the request omits `product_id`   |
| `LOW_RATING_THRESHOLD`   | `2`                              | Ratings at or below this trigger an alert   |
| `FEEDBACK_PAGE_LIMIT`    | `500`                            | Max items read per DynamoDB query/scan     |
| `HOST` / `PORT`          | `0.0.0.0` / `8000`               | Bind address for the built-in runner       |

## Run

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # optional, LocalStack
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 0.0.0.0 --port 8000
# or: python app.py
```

Example:

```bash
curl -X POST localhost:8000/feedback \
  -H 'Content-Type: application/json' \
  -d '{"product_id":"widget","rating":1,"comment":"Arrived broken"}'
curl localhost:8000/feedback
curl localhost:8000/feedback/stats?product_id=widget
```

## Tests

```bash
pytest
```

Tests are fully offline: the DynamoDB repository and SNS notifier are replaced by
in-memory fakes / injected doubles, so neither LocalStack nor network access is
needed. (FastAPI's `TestClient` requires `httpx`, which ships with the standard
FastAPI test tooling.)
