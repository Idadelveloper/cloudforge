# Notification Hub

FastAPI service that fans notification events out through a central SNS topic to
per-channel SQS queues, and stores subscriber records in DynamoDB.

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/health` | Reachability of SNS, SQS and DynamoDB |
| POST | `/events` | Publish an event to the central SNS topic |
| POST | `/subscriptions` | Register a subscriber (`email` or `webhook`) |
| GET | `/subscriptions` | List subscriptions (optional `?channel=`) |
| GET | `/subscriptions/{subscription_id}` | Fetch one subscription |
| PATCH | `/subscriptions/{subscription_id}` | Update target / event_types / active |
| DELETE | `/subscriptions/{subscription_id}` | Delete a subscription |
| GET | `/channels` | Channels and their backing queue URLs |
| GET | `/channels/stats` | Per-channel queue message counters |
| GET | `/channels/{channel}/messages` | Receive (and optionally delete) queued messages |

## Configuration

All settings come from environment variables:

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `AWS_ENDPOINT_URL` | _unset_ | Point every boto3 client at LocalStack (e.g. `http://localhost:4566`) |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `SNS_TOPIC_ARN` | resolved by name | Central topic ARN |
| `SNS_TOPIC_NAME` | `notification-hub-events-topic` | Used when the ARN is not supplied |
| `EMAIL_QUEUE_URL` | resolved by name | Email channel queue URL |
| `WEBHOOK_QUEUE_URL` | resolved by name | Webhook channel queue URL |
| `EMAIL_QUEUE_NAME` | `notification-hub-email-queue` | Email queue name |
| `WEBHOOK_QUEUE_NAME` | `notification-hub-webhook-queue` | Webhook queue name |
| `SUBSCRIPTIONS_TABLE` | `notification-hub-subscriptions` | DynamoDB table |
| `SUBSCRIPTIONS_CHANNEL_INDEX` | `channel-created_at-index` | GSI used to list by channel |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address when run directly |
| `LOG_LEVEL` | `INFO` | Logging level |

## Run locally

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs are served at `http://127.0.0.1:8000/docs`.

### Example

```bash
curl -X POST localhost:8000/subscriptions \
  -H 'content-type: application/json' \
  -d '{"channel":"email","target":"ops@example.com","event_types":["order.created"]}'

curl -X POST localhost:8000/events \
  -H 'content-type: application/json' \
  -d '{"event_type":"order.created","subject":"New order","payload":{"id":42}}'

curl localhost:8000/channels/stats
curl 'localhost:8000/channels/email/messages?delete=true'
```

## Tests

```bash
python -m pytest
```

The suite is fully offline: the API tests inject a fake repository and the
storage tests monkeypatch the boto3 client factories, so neither AWS nor
LocalStack is required.
