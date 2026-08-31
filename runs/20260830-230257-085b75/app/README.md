# URL Shortener API (`url_shortener_api`)

FastAPI service that turns long URLs into short base62 codes stored in DynamoDB.
Visiting a short code issues a `307` redirect and atomically increments the visit
counter; statistics are exposed per code.

## Endpoints

| Method | Path                 | Description                                              |
| ------ | -------------------- | -------------------------------------------------------- |
| GET    | `/health`            | Service status plus DynamoDB table reachability          |
| POST   | `/shorten`           | Body `{"url": "...", "custom_code": "optional"}` -> 201  |
| GET    | `/{code}`            | 307 redirect to the original URL (counter += 1)          |
| GET    | `/api/stats/{code}`  | `code`, `long_url`, `visit_count`, timestamps            |
| GET    | `/api/links`         | Paginated scan: `?limit=25&start=<code>`                 |
| DELETE | `/api/links/{code}`  | Remove a mapping                                         |

## Configuration

| Variable             | Default                   | Purpose                                        |
| -------------------- | ------------------------- | ---------------------------------------------- |
| `AWS_ENDPOINT_URL`   | (unset)                   | Point boto3 at LocalStack, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION` | `us-east-1`               | AWS region                                     |
| `MAPPINGS_TABLE_NAME`| `url_shortener_mappings`  | DynamoDB table (partition key `code`)          |
| `PUBLIC_BASE_URL`    | request base URL          | Prefix used when building `short_url`          |
| `SHORT_CODE_LENGTH`  | `7`                       | Generated code length                          |
| `HOST` / `PORT`      | `127.0.0.1` / `8000`      | Bind address when run directly                 |
| `LOG_LEVEL`          | `INFO`                    | Logging verbosity                              |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566   # LocalStack
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test

uvicorn app:app --host 127.0.0.1 --port 8000
```

Example session:

```bash
curl -X POST localhost:8000/shorten -H 'content-type: application/json' \
     -d '{"url": "https://example.com/some/long/path"}'
curl -i localhost:8000/<code>
curl localhost:8000/api/stats/<code>
```

## Tests

```bash
pytest -q
```

The suite is fully offline: a fake repository is injected through the FastAPI
dependency override and the DynamoDB layer is exercised against an in-process
stub table, so no AWS credentials, LocalStack instance or network access is
needed. (`fastapi.testclient.TestClient` relies on `httpx`, which ships with
FastAPI's testing extras.)
