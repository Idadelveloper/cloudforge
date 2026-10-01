# Personal Notes API

FastAPI REST service for personal notes backed by a single DynamoDB table.

## Endpoints

| Method | Path              | Description                                   |
| ------ | ----------------- | --------------------------------------------- |
| POST   | `/notes`          | Create a note (`title`, optional `body`)       |
| GET    | `/notes`          | List notes (`limit`, `cursor` query params)    |
| GET    | `/notes/{id}`     | Fetch one note, 404 when missing               |
| PUT    | `/notes/{id}`     | Partial update of `title` and/or `body`        |
| DELETE | `/notes/{id}`     | Delete a note, 204 on success                  |
| GET    | `/health`         | Liveness/readiness plus table reachability     |

## Configuration

| Variable              | Default     | Purpose                                       |
| --------------------- | ----------- | --------------------------------------------- |
| `NOTES_TABLE_NAME`    | `notes`     | DynamoDB table holding the notes               |
| `AWS_ENDPOINT_URL`    | _(unset)_   | Set to `http://localhost:4566` for LocalStack  |
| `AWS_DEFAULT_REGION`  | `us-east-1` | AWS region                                     |
| `HOST` / `PORT`       | `0.0.0.0` / `8000` | Bind address when run directly          |
| `LOG_LEVEL`           | `INFO`      | Root log level                                 |

## Running locally

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # LocalStack, omit for real AWS
export NOTES_TABLE_NAME=notes
uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs are served at `http://127.0.0.1:8000/docs`.

## Example

```bash
curl -X POST http://127.0.0.1:8000/notes \
  -H 'Content-Type: application/json' \
  -d '{"title": "groceries", "body": "milk, eggs"}'

curl http://127.0.0.1:8000/notes?limit=10
```

## Tests

```bash
ppython -m pytest
```

The suite runs completely offline: the HTTP layer is tested against an in-memory
repository injected through FastAPI dependency overrides, and the DynamoDB
repository is exercised against a fake boto3 table stub.
