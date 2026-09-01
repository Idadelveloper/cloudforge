# document_store

FastAPI backend for a versioned document store.

* Document binaries live in a versioning-enabled **S3** bucket
  (`documents/{document_id}/v{version}/{filename}`).
* Per-version metadata is indexed in the **DynamoDB** table `document-metadata`
  (PK `document_id`, SK `version`).
* Tag search is served by the **DynamoDB** table `document-tag-index`
  (PK `tag`, SK `document_id`), one item per (tag, document) pair.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AWS_ENDPOINT_URL` | *(unset)* | Custom endpoint, e.g. `http://localhost:4566` for LocalStack |
| `AWS_DEFAULT_REGION` | `us-east-1` | Region for all AWS clients |
| `DOCUMENTS_BUCKET` | `document-store-documents` | S3 bucket holding document versions |
| `METADATA_TABLE` | `document-metadata` | DynamoDB metadata table |
| `TAG_INDEX_TABLE` | `document-tag-index` | DynamoDB tag index table |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind address of the HTTP server |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # optional, LocalStack
export AWS_DEFAULT_REGION=us-east-1
python app.py
# or
uvicorn app:app --host 0.0.0.0 --port 8000
```

Interactive docs: `http://localhost:8000/docs`

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Reports S3 bucket / DynamoDB table reachability |
| POST | `/documents` | Upload a new document (version 1) |
| GET | `/documents?limit=&offset=` | List documents with latest-version metadata |
| GET | `/documents/search?tag=&limit=` | Search documents by tag |
| GET | `/documents/{document_id}` | Document summary (latest version + version count) |
| POST | `/documents/{document_id}/versions` | Upload a new version of an existing document |
| GET | `/documents/{document_id}/versions` | List all versions ordered by version number |
| GET | `/documents/{document_id}/versions/{version}/download?expires_in=` | Presigned S3 GET URL (default 900s, max 3600s) |
| DELETE | `/documents/{document_id}` | Delete every version, tag entry and S3 object version |

### Upload formats

Uploads accept three equivalent body flavours (parsed with the standard library,
so no extra dependencies are required):

1. `multipart/form-data` with a `file` part plus `title`, `author`, `tags`
   (comma separated or repeated fields):

   ```bash
   curl -F "file=@spec.pdf" -F "title=Spec" -F "author=ada" \
        -F "tags=design,api" http://localhost:8000/documents
   ```

2. JSON with base64 content:

   ```bash
   curl -H 'Content-Type: application/json' -d '{
     "title": "Spec", "author": "ada", "tags": ["design"],
     "filename": "spec.txt", "content_type": "text/plain",
     "content_base64": "aGVsbG8="
   }' http://localhost:8000/documents
   ```

3. Raw binary body with metadata in the query string:

   ```bash
   curl --data-binary @spec.pdf -H 'Content-Type: application/pdf' \
     'http://localhost:8000/documents?title=Spec&author=ada&tags=design&filename=spec.pdf'
   ```

## Tests

```bash
python -m pytest
```

Tests are fully offline: HTTP tests use the in-memory repository injected through
FastAPI dependency overrides, and the boto3 data-access layer is exercised with
local S3/DynamoDB stubs.
