# file_sharing_backend

FastAPI service that brokers file sharing through **S3** (object storage via presigned URLs)
and **DynamoDB** (file metadata + per-owner usage).

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/health` | Liveness plus the configured bucket/table/region. |
| POST | `/files/upload-url` | Create a presigned PUT URL and a `pending` metadata record. |
| POST | `/files/{file_id}/complete` | Read the real object size with HeadObject and mark it `available`. |
| GET | `/files?owner=&limit=&next_token=` | List an owner's files (owner-index query, paginated). |
| GET | `/files/{file_id}` | Metadata for one file plus a presigned GET download URL. |
| DELETE | `/files/{file_id}` | Hard delete the S3 object and the DynamoDB item. |
| GET | `/usage?owner=` | Storage usage for one owner, or every owner when omitted. |

## Configuration

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `AWS_ENDPOINT_URL` | _unset_ | Point every AWS client at LocalStack (e.g. `http://localhost:4566`). |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region. |
| `S3_BUCKET` | `file-sharing-objects` | Bucket holding uploaded objects. |
| `DYNAMODB_TABLE` | `file-metadata` | Metadata table (`file_id` partition key). |
| `DYNAMODB_OWNER_INDEX` | `owner-index` | GSI (`owner` hash, `uploaded_at` range). |
| `PRESIGN_EXPIRES_IN` | `900` | Presigned URL lifetime in seconds. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address for the built-in runner. |
| `LOG_LEVEL` | `INFO` | Root log level. |

## Run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1

uvicorn app:app --host 127.0.0.1 --port 8000
```

## Typical flow

```bash
# 1. ask for an upload URL
curl -s -X POST localhost:8000/files/upload-url \
  -H 'content-type: application/json' \
  -d '{"owner":"alice","filename":"notes.txt","content_type":"text/plain"}'

# 2. PUT the bytes straight to S3 using the returned upload_url
curl -s -X PUT --upload-file notes.txt -H 'content-type: text/plain' "$UPLOAD_URL"

# 3. confirm so the size/status are recorded
curl -s -X POST localhost:8000/files/$FILE_ID/complete

# 4. list and measure
curl -s "localhost:8000/files?owner=alice"
curl -s "localhost:8000/usage?owner=alice"
```

## Tests

Tests run fully offline: the API layer receives an in-memory `FileStore` through
`app.dependency_overrides`, and the AWS adapter is exercised with fake S3/DynamoDB doubles.

```bash
pip install -r requirements-dev.txt
pytest -q
```
