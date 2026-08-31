# url_shortener

A FastAPI URL shortener backed by DynamoDB.

## Endpoints

- `POST /shorten` — submit `{"url": "https://example.com"}` and receive a short code.
- `GET /{code}` — redirect (HTTP 302) to the original URL and increment the visit counter.
- `GET /stats/{code}` — return visit statistics for a code.

## Configuration

Environment variables:

- `AWS_ENDPOINT_URL` — override the AWS endpoint (e.g. LocalStack).
- `AWS_DEFAULT_REGION` — defaults to `us-east-1`.
- `URL_MAPPINGS_TABLE` — DynamoDB table name (default `url_mappings`).

## Running locally

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

The DynamoDB table must have a partition key `code` (string).

## Testing

```bash
pytest
```

Tests use an in-memory fake repository and require no network access.
