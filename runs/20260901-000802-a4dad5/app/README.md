# Event Registration Service

A FastAPI service where organisers create events (title, date, capacity) and
attendees register until the event is full. Events and registrations are stored
in DynamoDB; every successful registration is published as JSON to an SQS queue
for downstream processing.

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/health` | Liveness probe + configured resource names |
| POST | `/events` | Create an event (`title`, `date`, `capacity`) |
| GET | `/events` | List all events |
| GET | `/events/{event_id}` | Fetch one event (404 if unknown) |
| POST | `/events/{event_id}/registrations` | Register an attendee (201 / 409 full or duplicate / 404 unknown event) |
| GET | `/events/{event_id}/registrations` | List registrations for an event |
| GET | `/registrations/{registration_id}` | Fetch a single registration |

Capacity is enforced with a single conditional DynamoDB `UpdateItem`
(`registered_count < capacity`), so concurrent requests cannot oversell an
event. A conditional failure maps to HTTP 409.

## Configuration

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `AWS_ENDPOINT_URL` | *(unset)* | Set to e.g. `http://localhost:4566` for LocalStack |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `EVENTS_TABLE` | `events` | DynamoDB events table |
| `REGISTRATIONS_TABLE` | `registrations` | DynamoDB registrations table |
| `REGISTRATION_QUEUE_URL` | *(unset)* | Full SQS queue URL (resolved from the name when empty) |
| `REGISTRATION_QUEUE_NAME` | `registration-events` | SQS queue name |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 0.0.0.0 --port 8000
```

Example:

```bash
curl -X POST localhost:8000/events \
  -H 'content-type: application/json' \
  -d '{"title":"PyConf","date":"2030-05-01","capacity":2}'

curl -X POST localhost:8000/events/<event_id>/registrations \
  -H 'content-type: application/json' \
  -d '{"attendee_name":"Ada","attendee_email":"ada@example.com"}'
```

## Tests

The suite runs completely offline — the API tests inject in-memory repository
and publisher implementations, and the DynamoDB/SQS backends are exercised
against fakes.

```bash
pip install -r requirements-dev.txt
pytest
```
