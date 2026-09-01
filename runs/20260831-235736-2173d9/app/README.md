# Event Registration Service

A FastAPI backend where organisers create events (title, date, capacity) and
attendees register until the event is full. Events and registrations are stored
in DynamoDB and every successful registration is published to an SQS queue for
downstream processing.

## Endpoints

| Method | Path                              | Purpose                                              |
| ------ | --------------------------------- | ---------------------------------------------------- |
| GET    | `/health`                         | Liveness plus DynamoDB/SQS dependency status          |
| POST   | `/events`                         | Create an event (`title`, `date`, `capacity`)         |
| GET    | `/events`                         | List events (`limit`, `cursor`)                       |
| GET    | `/events/{event_id}`              | Event detail incl. remaining capacity (404 if absent) |
| POST   | `/events/{event_id}/registrations`| Register an attendee (409 when full or duplicate)     |
| GET    | `/events/{event_id}/registrations`| List registrations for an event                       |

Capacity is enforced with a conditional atomic `UpdateItem`
(`registered_count < capacity`), so concurrent requests cannot oversell an
event; a failed condition returns `409 Conflict`.

## Configuration

| Variable                 | Default               | Description                                   |
| ------------------------ | --------------------- | --------------------------------------------- |
| `AWS_ENDPOINT_URL`       | _unset_               | Set to `http://localhost:4566` for LocalStack |
| `AWS_DEFAULT_REGION`     | `us-east-1`           | AWS region                                     |
| `EVENTS_TABLE`           | `events`              | DynamoDB table for events                      |
| `REGISTRATIONS_TABLE`    | `registrations`       | DynamoDB table for registrations               |
| `REGISTRATION_QUEUE`     | `registration-events` | SQS queue name (resolved to a URL at runtime)  |
| `REGISTRATION_QUEUE_URL` | _unset_               | Explicit queue URL (skips `GetQueueUrl`)       |
| `HOST` / `PORT`          | `127.0.0.1` / `8000`  | Bind address for the built-in runner           |
| `LOG_LEVEL`              | `INFO`                | Logging level                                  |

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test

uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs are served at <http://127.0.0.1:8000/docs>.

### Example

```bash
curl -X POST localhost:8000/events \
  -H 'content-type: application/json' \
  -d '{"title":"CloudForge Day","date":"2025-06-01","capacity":2}'

curl -X POST localhost:8000/events/<event_id>/registrations \
  -H 'content-type: application/json' \
  -d '{"attendee_name":"Ada","attendee_email":"ada@example.com"}'
```

## Tests

```bash
pytest
```

The suite runs fully offline: API tests use an in-memory repository/publisher,
and the DynamoDB and SQS layers are exercised against local fakes.
