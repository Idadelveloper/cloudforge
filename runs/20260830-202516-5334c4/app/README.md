# Personal Notes API

A small FastAPI service that stores personal notes in DynamoDB.

## Endpoints

| Method | Path              | Description                                        |
|--------|-------------------|----------------------------------------------------|
| GET    | `/health`         | Liveness/readiness probe                           |
| POST   | `/notes`          | Create a note (`title`, `body`) → 201 with the note |
| GET    | `/notes`          | List notes (`limit` 1..100, `next_token` cursor)   |
| GET    | `/notes/{id}`     | Fetch one note (404 when missing)                  |
| PUT    | `/notes/{id}`     | Replace `title`/`body`, refresh `updated_at`       |
| DELETE | `/notes/{id}`     | Delete a note → 204 (404 when missing)             |

Errors are returned as `{"detail": "...", "code": "..."}`.

## Configuration

| Variable             | Default        | Purpose                                              |
|----------------------|----------------|------------------------------------------------------|
| `NOTES_TABLE_NAME`   | `notes-table`  | DynamoDB table (PK `owner_id`, SK `note_id`)         |
| `AWS_ENDPOINT_URL`   | _unset_        | Set to `http://localhost:4566` for LocalStack        |
| `AWS_DEFAULT_REGION` | `us-east-1`    | AWS region                                           |
| `NOTES_OWNER_ID`     | `default-user` | Owner partition used for all notes (single tenant)   |
| `NOTES_BACKEND`      | `dynamodb`     | Set to `memory` to run without AWS                   |
| `HOST` / `PORT`      | `127.0.0.1` / `8000` | Local uvicorn bind address                     |
| `LOG_LEVEL`          | `INFO`         | Root log level                                        |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# no AWS needed:
NOTES_BACKEND=memory python app.py

# against LocalStack DynamoDB:
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export NOTES_TABLE_NAME=notes-table
python app.py
```

Interactive docs: <http://127.0.0.1:8000/docs>

Example:

```bash
curl -X POST http://127.0.0.1:8000/notes \
  -H 'Content-Type: application/json' \
  -d '{"title": "Groceries", "body": "milk, eggs"}'

curl 'http://127.0.0.1:8000/notes?limit=25'
```

## Lambda / API Gateway

`lambda_handler.lambda_handler` is a stdlib-only API Gateway (AWS_PROXY) to ASGI
adapter, so the same application can be zipped and deployed as
`lambda_handler.lambda_handler` behind API Gateway.

## Tests

```bash
python -m pytest -q
```

All AWS calls are stubbed, so the suite runs completely offline.
