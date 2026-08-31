# Personal Notes API

A FastAPI REST service for personal notes, persisted in a DynamoDB table keyed by
`user_id` (partition key) and `note_id` (sort key).

## Endpoints

| Method | Path              | Description                                        |
|--------|-------------------|----------------------------------------------------|
| GET    | `/health`         | Liveness/readiness probe (no AWS calls)            |
| POST   | `/notes`          | Create a note (`title`, `body`)                    |
| GET    | `/notes`          | List caller notes (`limit`, `next_token`)          |
| GET    | `/notes/{id}`     | Fetch one note, 404 if absent                      |
| PUT    | `/notes/{id}`     | Partial update of `title`/`body`, refreshes `updated_at` |
| DELETE | `/notes/{id}`     | Delete a note, 204 on success, 404 if absent       |

The caller is identified by the optional `X-User-Id` header; requests without it
are scoped to `default-user`. Timestamps are server-generated ISO-8601 UTC.

## Configuration

| Variable             | Default        | Purpose                                     |
|----------------------|----------------|---------------------------------------------|
| `NOTES_TABLE_NAME`   | `notes-table`  | DynamoDB table name                         |
| `AWS_ENDPOINT_URL`   | _(unset)_      | Point boto3 at LocalStack, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION` | `us-east-1`    | AWS region                                  |
| `DEFAULT_USER_ID`    | `default-user` | Fallback tenant when no header is supplied  |
| `HOST` / `PORT`      | `127.0.0.1` / `8000` | Local uvicorn bind address            |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export NOTES_TABLE_NAME=notes-table

uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs are served at <http://127.0.0.1:8000/docs>.

Example:

```bash
curl -X POST http://127.0.0.1:8000/notes \
  -H 'Content-Type: application/json' -H 'X-User-Id: alice' \
  -d '{"title": "Shopping", "body": "Milk and eggs"}'
```

## Tests

```bash
pip install -r requirements.txt pytest
pytest
```

Tests run fully offline: the HTTP layer uses `InMemoryNotesRepository` via FastAPI
dependency overrides, and the DynamoDB repository is exercised against an
in-process fake boto3 table. No LocalStack or network access is required.

## Deployment notes

The application object is a plain ASGI app (`app:app`), so it can be served by
uvicorn on a container/EC2 host or wrapped by an ASGI-to-Lambda adapter (added at
deploy time) behind API Gateway. The Lambda execution role only needs
`GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` and `Query` on the notes table
plus CloudWatch Logs write access.
