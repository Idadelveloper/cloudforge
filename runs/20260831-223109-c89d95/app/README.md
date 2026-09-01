# file_share_backend

FastAPI service that brokers file sharing through S3 (objects) and DynamoDB (metadata).
Clients request a presigned S3 `PUT` URL, upload the bytes directly to S3, then confirm
the upload so metadata is recorded. Files can be listed, downloaded via presigned `GET`
URLs, deleted, and aggregated into per-owner storage usage.

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/health` | Liveness plus S3/DynamoDB connectivity |
| POST | `/files/upload-url` | Create pending record + presigned PUT URL |
| POST | `/files/{file_id}/confirm` | Verify object in S3, record real size, mark available |
| GET | `/files?owner=...` | List an owner's files (`limit`, `next_token`) |
| GET | `/files/{file_id}` | Single file metadata |
| GET | `/files/{file_id}/download-url` | Presigned GET URL |
| DELETE | `/files/{file_id}` | Delete S3 object + metadata record |
| GET | `/usage[?owner=...]` | Storage usage per owner |

## Configuration

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `AWS_ENDPOINT_URL` | (unset) | Point boto3 at LocalStack, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `FILE_SHARE_BUCKET` | `file-share-files` | S3 bucket for objects |
| `FILE_SHARE_TABLE` | `file-share-metadata` | DynamoDB metadata table |
| `FILE_SHARE_OWNER_INDEX` | `owner-index` | GSI used for list/usage queries |
| `PRESIGNED_URL_EXPIRY_SECONDS` | `900` | Presigned URL lifetime |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address for the dev server |
| `LOG_LEVEL` | `INFO` | Logging level |

## Run

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs are served at `http://127.0.0.1:8000/docs`.

## Typical flow

```bash
curl -X POST localhost:8000/files/upload-url \
  -H 'content-type: application/json' \
  -d '{"owner":"alice","filename":"report.pdf","content_type":"application/pdf"}'
# PUT the bytes to the returned upload_url, then:
curl -X POST localhost:8000/files/<file_id>/confirm
curl 'localhost:8000/files?owner=alice'
curl 'localhost:8000/usage?owner=alice'
```

## Tests

```bash
python -m pytest -q
```

Tests run fully offline: the DynamoDB/S3 layers are replaced with in-memory fakes and
stub boto3 clients, so no LocalStack or network access is required.
