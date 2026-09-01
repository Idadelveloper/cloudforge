# contact_form_backend

JSON REST API for a public contact form. Visitors submit `name`, `email` and
`message`; administrators list, retrieve and delete stored messages. Messages
are persisted in DynamoDB.

## Endpoints

| Method | Path                   | Auth                 | Purpose                                  |
| ------ | ---------------------- | -------------------- | ---------------------------------------- |
| GET    | `/health`              | none                 | Liveness + DynamoDB reachability         |
| POST   | `/messages`            | none (public)        | Submit a contact message                 |
| GET    | `/messages`            | `X-Admin-API-Key`    | List messages (`limit`, `next_token`)    |
| GET    | `/messages/{id}`       | `X-Admin-API-Key`    | Fetch one message (404 if absent)        |
| DELETE | `/messages/{id}`       | `X-Admin-API-Key`    | Delete one message (404 if absent)       |

## Configuration

| Variable                   | Default                       | Meaning                                     |
| -------------------------- | ----------------------------- | ------------------------------------------- |
| `AWS_ENDPOINT_URL`         | unset (real AWS)              | Set to `http://localhost:4566` for LocalStack |
| `AWS_DEFAULT_REGION`       | `us-east-1`                   | AWS region                                  |
| `MESSAGES_TABLE`           | `contact-form-messages`       | DynamoDB table (partition key `message_id`) |
| `ADMIN_API_KEY_SECRET_NAME`| `contact-form/admin-api-key`  | Secrets Manager secret holding the admin key |
| `ADMIN_API_KEY`            | unset                         | Overrides the secret (useful locally/tests) |
| `HOST` / `PORT`            | `127.0.0.1` / `8000`          | Bind address for the uvicorn entrypoint      |
| `LOG_LEVEL`                | `INFO`                        | Logging level                               |

The admin key is read from `ADMIN_API_KEY` when present, otherwise from
Secrets Manager (cached in-process). Admin endpoints return `503` when no key
is configured and `401` when the header is missing or wrong.

## Run locally

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export MESSAGES_TABLE=contact-form-messages
export ADMIN_API_KEY="$(openssl rand -hex 16)"
python app.py            # or: uvicorn app:app --host 127.0.0.1 --port 8000
```

Example requests:

```bash
curl -X POST localhost:8000/messages \
  -H 'Content-Type: application/json' \
  -d '{"name":"Ada","email":"ada@example.com","message":"Hello!"}'

curl localhost:8000/messages -H "X-Admin-API-Key: $ADMIN_API_KEY"
curl -X DELETE localhost:8000/messages/<id> -H "X-Admin-API-Key: $ADMIN_API_KEY"
```

## Tests

```bash
pytest
```

Tests run fully offline: the repository is injected via FastAPI dependency
overrides and the DynamoDB layer is exercised against an in-process fake table.
