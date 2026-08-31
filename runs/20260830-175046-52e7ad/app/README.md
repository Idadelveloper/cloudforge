# personal_notes_api

REST API for personal notes (create, list, fetch, update, delete) backed by a
single DynamoDB table. Built with FastAPI; the HTTP layer talks only to the
`NotesRepository` interface in `storage.py`, so persistence is swappable.

## Endpoints

| Method | Path              | Description                                        |
|--------|-------------------|----------------------------------------------------|
| GET    | `/health`         | Liveness/readiness probe                           |
| POST   | `/notes`          | Create a note (`title` required, `body` optional)  |
| GET    | `/notes`          | List caller's notes (`limit` 1-100, `cursor`)      |
| GET    | `/notes/{id}`     | Fetch one note (404 when absent)                   |
| PUT    | `/notes/{id}`     | Partial update of `title`/`body`, refresh timestamp|
| DELETE | `/notes/{id}`     | Delete a note (204 on success, 404 when absent)    |

The optional `X-User-Id` request header identifies the note owner; when absent,
notes are stored under `default-user`.

## Configuration

| Variable              | Default        | Purpose                                  |
|-----------------------|----------------|------------------------------------------|
| `NOTES_TABLE_NAME`    | `notes-table`  | DynamoDB table name                      |
| `AWS_ENDPOINT_URL`    | _(unset)_      | Set to e.g. `http://localhost:4566` for LocalStack |
| `AWS_DEFAULT_REGION`  | `us-east-1`    | AWS region                               |
| `DEFAULT_USER_ID`     | `default-user` | Partition value used without auth context|
| `HOST` / `PORT`       | `127.0.0.1` / `8000` | Local dev bind address             |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# optional: point at LocalStack and create the table
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
aws --endpoint-url "$AWS_ENDPOINT_URL" dynamodb create-table \
  --table-name notes-table \
  --attribute-definitions AttributeName=user_id,AttributeType=S AttributeName=note_id,AttributeType=S \
  --key-schema AttributeName=user_id,KeyType=HASH AttributeName=note_id,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST

uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs: <http://127.0.0.1:8000/docs>

### Example

```bash
curl -X POST http://127.0.0.1:8000/notes \
  -H 'Content-Type: application/json' -H 'X-User-Id: alice' \
  -d '{"title": "Groceries", "body": "milk, bread"}'

curl 'http://127.0.0.1:8000/notes?limit=25' -H 'X-User-Id: alice'
```

## Tests

Tests are fully offline: DynamoDB is replaced by an in-memory repository or a
fake table object, so no LocalStack or network access is required.

```bash
pip install -r requirements-dev.txt   # adds pytest + httpx (needed by TestClient)
pytest -q
```

## Deployment notes

The app is designed to run as a Lambda function behind API Gateway using an
ASGI adapter in the deployment package (the adapter is not vendored here to keep
runtime dependencies minimal). The Lambda execution role needs
`dynamodb:GetItem/PutItem/UpdateItem/DeleteItem/Query` on the notes table plus
CloudWatch Logs write access.
