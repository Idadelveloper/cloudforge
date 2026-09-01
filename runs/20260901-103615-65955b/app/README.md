# Notification Hub

FastAPI service that fans notification events out from a central SNS topic to one
SQS queue per delivery channel (`email` and `webhook`), with subscription records
stored in DynamoDB.

## AWS resources (created by the infrastructure stack)

| Resource | Default name | Env override |
| --- | --- | --- |
| SNS topic | `notification-hub-events` | `SNS_TOPIC_NAME` / `SNS_TOPIC_ARN` |
| SQS email queue | `notification-hub-email-queue` | `EMAIL_QUEUE_NAME` |
| SQS webhook queue | `notification-hub-webhook-queue` | `WEBHOOK_QUEUE_NAME` |
| DynamoDB table | `notification-hub-subscriptions` | `DYNAMODB_TABLE_NAME` |
| Channel GSI | `channel-index` | `SUBSCRIPTIONS_CHANNEL_INDEX` |

All clients honour `AWS_ENDPOINT_URL` (LocalStack) and default to region
`us-east-1` (`AWS_DEFAULT_REGION`).

## Run

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 127.0.0.1 --port 8000
# or: python app.py   (HOST / PORT env vars)
```

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness plus SNS/SQS/DynamoDB connectivity |
| POST | `/subscriptions` | Register a subscriber (`channel`, `target`, `event_types`) |
| GET | `/subscriptions` | List subscriptions (`?channel=`, `?target=`) |
| GET | `/subscriptions/{id}` | Fetch one subscription |
| DELETE | `/subscriptions/{id}` | Remove a subscription |
| POST | `/events` | Publish an event to the SNS topic |
| GET | `/channels` | Channels with their queue URL/ARN |
| GET | `/channels/{channel}/messages` | Peek/drain a channel queue (`?delete=true`) |
| GET | `/stats` | Approximate per-channel message counts |

Example:

```bash
curl -X POST localhost:8000/subscriptions \
  -H 'content-type: application/json' \
  -d '{"channel":"email","target":"ops@example.com","event_types":["order.created"]}'

curl -X POST localhost:8000/events \
  -H 'content-type: application/json' \
  -d '{"event_type":"order.created","payload":{"order_id":"42"}}'

curl localhost:8000/stats
```

## Tests

```bash
python -m pytest -q
```

The test-suite is fully offline: the HTTP layer runs against an injected fake
repository and the storage layer runs against stub SNS/SQS/DynamoDB objects.
