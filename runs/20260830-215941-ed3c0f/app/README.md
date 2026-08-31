# Personal Notes API

A small FastAPI service exposing CRUD endpoints for personal notes stored in a
single DynamoDB table (`notes`, string partition key `id`).

## Endpoints

| Method | Path              | Description                                      |
| ------ | ----------------- | ------------------------------------------------ |
| GET    | `/health`         | Service status + DynamoDB table reachability     |
| POST   | `/notes`          | Create a note (`{"title": "...", "body": "..."}`)|
| GET    | `/notes`          | List notes (`?limit=50&next_token=...`)          |
| GET    | `/notes/{id}`     | Fetch a note (404 when missing)                  |
| PUT    | `/notes/{id}`     | Partial update of title/body (404 when missing)  |
| DELETE | `/notes/{id}`     | Delete a note (204, 404 when missing)            |

Validation errors return `422`, bad pagination cursors return `400`.

## Configuration

| Variable              | Default     | Purpose                                     |
| --------------------- | ----------- | ------------------------------------------- |
| `AWS_ENDPOINT_URL`    | _unset_     | Point boto3 at LocalStack, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION`  | `us-east-1` | AWS region                                  |
| `NOTES_TABLE_NAME`    | `notes`     | DynamoDB table name                         |
| `HOST` / `PORT`       | `127.0.0.1` / `8000` | Bind address for the dev server    |
| `LOG_LEVEL`           | `INFO`      | Application log level                       |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export NOTES_TABLE_NAME=notes

uvicorn app:app --host 127.0.0.1 --port 8000
# or: python app.py
```

Interactive docs are available at `http://127.0.0.1:8000/docs`.

## Tests

Tests run fully offline; every AWS call is replaced by an in-memory fake.

```bash
ppython -m pytest -q
```
