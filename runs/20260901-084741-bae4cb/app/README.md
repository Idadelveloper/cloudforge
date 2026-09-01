# Document Store Service

FastAPI backend that stores document binaries in a **versioned S3 bucket** and
indexes metadata (title, author, tags, version) in **DynamoDB**. Clients can
upload documents and new versions, list versions, search by tag through a
DynamoDB GSI, and get a time-limited presigned S3 URL for a specific version.

## Layout

| File | Purpose |
| --- | --- |
| `app.py` | FastAPI application and HTTP routes |
| `storage.py` | AWS access layer (S3 + DynamoDB behind a repository interface) plus an in-memory implementation |
| `uploads.py` | Small stdlib `multipart/form-data` parser |
| `tests/test_app.py` | Offline tests (no AWS/network required) |

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AWS_ENDPOINT_URL` | _(unset)_ | Override endpoint for all AWS clients (LocalStack) |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `DOCUMENTS_BUCKET` | `document-store-documents` | Versioned S3 bucket for binaries |
| `DOCUMENTS_TABLE` | `documents-metadata` | DynamoDB metadata table (`document_id` HASH, `version` RANGE) |
| `TAG_INDEX_NAME` | `tag-index` | GSI used for tag search (`tag`/`created_at`) |
| `AUTHOR_INDEX_NAME` | `author-index` | GSI used for author filtering |
| `APP_CONFIG_SECRET_NAME` | `document-store/app-config` | Secrets Manager secret holding `{"api_key": "..."}` |
| `DOCUMENT_STORE_API_KEY` | _(unset)_ | Direct API key override; when neither this nor the secret resolves, write endpoints are unauthenticated |
| `LOAD_APP_CONFIG_SECRET` | auto (`true` when `AWS_ENDPOINT_URL` set) | Whether to read the API key from Secrets Manager |
| `PRESIGN_DEFAULT_EXPIRY` | `900` | Default presigned URL TTL (seconds) |
| `PRESIGN_MAX_EXPIRY` | `3600` | Maximum presigned URL TTL |
| `MAX_UPLOAD_BYTES` | `10485760` | Maximum upload size (10 MB) |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address for the built-in runner |

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # LocalStack
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 0.0.0.0 --port 8000
```

(or `HOST=0.0.0.0 PORT=8000 python app.py`)

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Reports S3 / DynamoDB reachability |
| POST | `/documents` | Upload a document (multipart `title`,`author`,`tags`,`file` **or** JSON `{title, author, tags, filename, content_type, content_base64}`) |
| GET | `/documents` | List latest versions, `?author=`, `?limit=`, `?next_token=` |
| POST | `/documents/{document_id}/versions` | Upload a new version (same body formats) |
| GET | `/documents/{document_id}/versions` | All versions with sizes, S3 version ids, timestamps |
| GET | `/documents/{document_id}/versions/{version}` | Metadata for one version |
| GET | `/documents/{document_id}/versions/{version}/download-url` | Presigned GET URL, `?expires_in=` (max 3600s) |
| GET | `/search?tag=` | Tag search via the DynamoDB GSI (scan fallback for secondary tags) |
| DELETE | `/documents/{document_id}` | Deletes all metadata items and all S3 object versions |

Write endpoints (`POST`, `DELETE`) require the `X-API-Key` header when an API key
is configured.

## Tests

```bash
python -m pytest tests -q
```

Tests use the FastAPI `TestClient` with an injected in-memory repository plus
fake boto3 stubs, so they run fully offline.
