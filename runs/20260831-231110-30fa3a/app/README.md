# bookmark_manager_api

A FastAPI service that stores bookmarks (URL, title, tags) in DynamoDB and
protects every data endpoint with a shared API key that is read from AWS
Secrets Manager.

## Endpoints

| Method | Path                     | Auth | Purpose |
| ------ | ------------------------ | ---- | ------- |
| GET    | `/health`                | no   | Liveness/readiness + DynamoDB reachability |
| POST   | `/bookmarks`             | yes  | Create a bookmark (`url`, `title`, optional `tags`) |
| GET    | `/bookmarks`             | yes  | List bookmarks, optional `tag`, `limit` (1-200, default 50), `next_token` |
| GET    | `/bookmarks/{id}`        | yes  | Fetch one bookmark (404 if unknown) |
| DELETE | `/bookmarks/{id}`        | yes  | Delete one bookmark (404 if unknown) |

Authenticated requests must send the key in the `X-API-Key` header. A missing
header returns `401`, a wrong value returns `403`. Errors are returned as
`{"detail": "...", "status_code": n}`.

## Configuration

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `AWS_ENDPOINT_URL` | unset | Endpoint override for LocalStack (e.g. `http://localhost:4566`) |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `BOOKMARKS_TABLE` | `bookmarks` | DynamoDB table (PK `bookmark_id`) |
| `BOOKMARK_TAGS_TABLE` | `bookmark_tags` | Tag index table (PK `tag`, SK `bookmark_id`) |
| `API_KEY_SECRET_ID` | `bookmark-manager/api-key` | Secrets Manager id holding `{"api_key": "..."}` (plain strings also accepted) |
| `API_KEY` | unset | Fallback key used only when Secrets Manager is unreachable |
| `PRELOAD_API_KEY` | `true` | Set to `false` to skip the startup secret fetch |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address when started via `python app.py` |
| `LOG_LEVEL` | `INFO` | Root log level |

## Running locally against LocalStack

```bash
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test

uvicorn app:app --host 0.0.0.0 --port 8000
```

(or `HOST=0.0.0.0 python app.py`)

Example calls:

```bash
curl localhost:8000/health

curl -X POST localhost:8000/bookmarks \
  -H "X-API-Key: $MY_KEY" -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","title":"Example","tags":["Docs"]}'

curl "localhost:8000/bookmarks?tag=docs&limit=10" -H "X-API-Key: $MY_KEY"
```

## Tests

```bash
python -m pytest
```

The suite is fully offline: the HTTP layer runs against an in-memory repository
and a static key provider, while the DynamoDB and Secrets Manager code paths are
exercised with stub clients. No LocalStack or network access is required.
(The FastAPI `TestClient` needs `httpx`, which ships with FastAPI's test extras.)

## Layout

- `app.py` – FastAPI application, routes, validation and API-key dependency.
- `storage.py` – boto3 clients, repository interface, DynamoDB + in-memory
  implementations, Secrets Manager key provider, pagination cursors.
- `tests/test_app.py` – endpoint and storage tests.
