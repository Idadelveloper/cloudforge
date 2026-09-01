# contact_form_backend

JSON REST backend for a website contact form. Visitors submit messages; admins
list, fetch and delete them. Messages are stored in a DynamoDB table.

## Endpoints

| Method | Path                   | Auth        | Purpose                                        |
| ------ | ---------------------- | ----------- | ---------------------------------------------- |
| GET    | `/health`              | public      | Service status + DynamoDB reachability         |
| POST   | `/messages`            | public      | Submit `{name, email, message}` (201 Created)  |
| GET    | `/messages`            | admin token | List messages, newest first (`limit`, `next_token`) |
| GET    | `/messages/{id}`       | admin token | Fetch one message (404 if unknown)             |
| DELETE | `/messages/{id}`       | admin token | Delete one message (204 / 404)                 |

Admin endpoints require the header `X-Admin-Token: <ADMIN_TOKEN>`; a missing or
wrong value returns 401. Errors use the envelope `{"detail": ..., "code": ...}`.

## Environment variables

| Variable            | Default             | Meaning                                    |
| ------------------- | ------------------- | ------------------------------------------ |
| `TABLE_NAME`        | `contact-messages`  | DynamoDB table (partition key `id`)        |
| `AWS_ENDPOINT_URL`  | _unset_             | Set to e.g. `http://localhost:4566` for LocalStack |
| `AWS_REGION`        | `us-east-1`         | AWS region                                 |
| `ADMIN_TOKEN`       | `cloudforge-admin`  | Shared admin credential                    |
| `HOST` / `PORT`     | all interfaces / `8000` | Bind address when run directly         |
| `LOG_LEVEL`         | `INFO`              | Logging verbosity                          |

## Run locally

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # LocalStack (optional)
export AWS_REGION=us-east-1
export TABLE_NAME=contact-messages
export ADMIN_TOKEN=my-admin-value
uvicorn app:app --host 127.0.0.1 --port 8000
# or: python app.py
```

Example submission:

```bash
curl -X POST localhost:8000/messages -H 'Content-Type: application/json' \
  -d '{"name":"Ada","email":"ada@example.com","message":"Hello!"}'

curl localhost:8000/messages -H "X-Admin-Token: $ADMIN_TOKEN"
```

## Tests

```bash
python -m pytest
```

Tests are fully offline: the DynamoDB layer is replaced by an injected fake
repository / fake table, so no AWS account or LocalStack instance is needed.
