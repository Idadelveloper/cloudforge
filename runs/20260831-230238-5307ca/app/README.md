# Bookmark Manager API

A FastAPI service that stores bookmarks (URL, title, tags) in DynamoDB. Every data
endpoint requires the shared API key in the `X-API-Key` header; the expected key is
read from AWS Secrets Manager and cached in-process.

## Endpoints

| Method | Path                    | Auth | Description                                        |
| ------ | ----------------------- | ---- | -------------------------------------------------- |
| GET    | `/health`               | no   | Liveness probe                                      |
| POST   | `/bookmarks`            | yes  | Create a bookmark (`url`, `title`, optional `tags`) |
| GET    | `/bookmarks`            | yes  | List bookmarks, optional `?tag=` and `?limit=`      |
| GET    | `/bookmarks/{id}`       | yes  | Fetch one bookmark (404 when missing)               |
| DELETE | `/bookmarks/{id}`       | yes  | Delete one bookmark (204 / 404)                     |

Missing header -> `401`, wrong key -> `403`.

## Configuration

| Variable               | Default                     | Purpose                                   |
| ---------------------- | --------------------------- | ----------------------------------------- |
| `AWS_ENDPOINT_URL`     | _(unset)_                   | Point boto3 at LocalStack, e.g. `http://localhost:4566` |
| `AWS_REGION`           | `us-east-1`                 | AWS region                                 |
| `BOOKMARKS_TABLE`      | `bookmarks`                 | DynamoDB table name                        |
| `BOOKMARKS_TAG_INDEX`  | `bookmarks-tag-index`       | GSI name for the tag projection            |
| `API_KEY_SECRET_NAME`  | `bookmark-manager/api-key`  | Secrets Manager secret id                  |
| `API_KEY_CACHE_TTL`    | `300`                       | Seconds to cache the fetched key           |
| `HOST` / `PORT`        | `127.0.0.1` / `8000`        | Bind address when running `python app.py`  |

The secret may be JSON (`{"api_key": "..."}`) or a plain string.

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_REGION=us-east-1
uvicorn app:app --host 0.0.0.0 --port 8000
```

Example request:

```bash
curl -X POST http://localhost:8000/bookmarks \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com", "title": "Example", "tags": ["Docs"]}'
```

## Tests

```bash
pytest
```

The test suite runs fully offline: the repository and the API key provider are
replaced by in-memory fakes and boto3 is monkeypatched.
