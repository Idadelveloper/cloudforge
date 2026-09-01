# contact_form_backend

JSON REST API for a website contact form. Public visitors submit messages
(name, email, body); administrators list, read and delete them. Messages live in
a DynamoDB table; the administrator API key is read from Secrets Manager.

## Endpoints

| Method | Path                  | Auth        | Purpose                                  |
| ------ | --------------------- | ----------- | ---------------------------------------- |
| GET    | `/health`             | none        | Liveness + DynamoDB reachability         |
| POST   | `/messages`           | none        | Submit a contact message                 |
| GET    | `/messages`           | `X-Api-Key` | List messages (`limit`, `cursor` params) |
| GET    | `/messages/{id}`      | `X-Api-Key` | Read one message (404 if missing)        |
| DELETE | `/messages/{id}`      | `X-Api-Key` | Delete one message (404 if missing)      |

Errors are returned as `{"detail": "...", "code": "..."}`.

## Configuration

| Variable                    | Default                        | Meaning                              |
| --------------------------- | ------------------------------ | ------------------------------------ |
| `AWS_ENDPOINT_URL`          | unset                          | Endpoint override (LocalStack)       |
| `AWS_DEFAULT_REGION`        | `us-east-1`                    | AWS region                           |
| `MESSAGES_TABLE_NAME`       | `contact-form-messages`        | DynamoDB table                       |
| `ADMIN_API_KEY_SECRET_NAME` | `contact-form/admin-api-key`   | Secrets Manager secret id            |
| `ADMIN_API_KEY`             | unset                          | Overrides the secret (local dev)     |
| `HOST` / `PORT`             | `127.0.0.1` / `8000`           | Bind address                         |
| `LOG_LEVEL`                 | `INFO`                         | Logging level                        |

## Run

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # when using LocalStack
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 127.0.0.1 --port 8000
```

Example submission:

```bash
curl -X POST http://127.0.0.1:8000/messages \
  -H 'Content-Type: application/json' \
  -d '{"name":"Ada","email":"ada@example.com","message":"Hello!"}'
```

Admin listing:

```bash
curl -H "X-Api-Key: $ADMIN_API_KEY" 'http://127.0.0.1:8000/messages?limit=25'
```

## Tests

```bash
pytest
```

Tests run fully offline: the DynamoDB and Secrets Manager layers are replaced
with in-memory fakes through FastAPI dependency overrides.
