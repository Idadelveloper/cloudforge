# url_shortener

A FastAPI service that turns long URLs into short codes, redirects visitors and
tracks visit counts. Mappings live in a DynamoDB table (`url_shortener_urls` by
default) accessed through boto3.

## Endpoints

| Method | Path                 | Description |
| ------ | -------------------- | ----------- |
| POST   | `/urls`              | Create a short code (`{"url": "...", "custom_code": "optional"}`) |
| GET    | `/{code}`            | 307 redirect to the long URL, atomically increments the visit counter |
| GET    | `/urls/{code}/stats` | Visit statistics for a code |
| GET    | `/urls`              | Paginated list of mappings (`limit`, `start_after`) |
| DELETE | `/urls/{code}`       | Delete a mapping |
| GET    | `/health`            | Service status + DynamoDB reachability |

## Configuration

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `AWS_ENDPOINT_URL` | unset | Point boto3 at LocalStack, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `URL_TABLE_NAME` | `url_shortener_urls` | DynamoDB table name |
| `SHORT_URL_BASE_URL` | `http://localhost:8000` | Base used to build returned short URLs |
| `SHORT_CODE_LENGTH` | `7` | Length of generated codes |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address when run via `python app.py` |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Run locally (against LocalStack)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test

uvicorn app:app --host 127.0.0.1 --port 8000
```

The DynamoDB table (partition key `code`, string) is created by the
infrastructure code; the app only reads and writes it.

### Example

```bash
curl -s -X POST localhost:8000/urls -H 'content-type: application/json' \
  -d '{"url": "https://example.com/some/long/path"}'
# {"code":"Ab3xY7z", ...}

curl -si localhost:8000/Ab3xY7z | head -n 1   # HTTP/1.1 307 Temporary Redirect
curl -s localhost:8000/urls/Ab3xY7z/stats
```

## Tests

```bash
pip install -r requirements.txt pytest
pytest -q
```

Tests are fully offline: the API tests inject an in-memory repository and the
storage tests drive `DynamoUrlRepository` with a fake DynamoDB table object.
