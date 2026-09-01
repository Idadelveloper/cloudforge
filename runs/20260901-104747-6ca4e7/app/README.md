# Blog Platform Backend

FastAPI service for a small blog: markdown posts in DynamoDB, images in S3 and
reader comments moderated through an SQS queue before they are published.

## Layout

- `app.py` — HTTP routes, request/response models, entrypoint.
- `storage.py` — boto3 data-access layer (DynamoDB, S3, SQS) behind small classes.
- `uploads.py` — stdlib multipart / raw body parsing for image uploads.
- `tests/test_app.py` — offline tests using `fastapi.testclient.TestClient` with fakes.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `AWS_ENDPOINT_URL` | unset | Endpoint override (e.g. `http://localhost:4566` for LocalStack) |
| `POSTS_TABLE` | `blog-posts` | Posts DynamoDB table |
| `COMMENTS_TABLE` | `blog-published-comments` | Published comments table |
| `IMAGES_BUCKET` | `blog-post-images` | S3 bucket for post images |
| `MODERATION_QUEUE` | `blog-comment-moderation-queue` | SQS queue name |
| `MODERATION_QUEUE_URL` | resolved from the name | Explicit queue URL |
| `PRESIGN_TTL` | `3600` | Presigned GET URL lifetime (seconds) |
| `MAX_IMAGE_BYTES` | `10485760` | Upload size limit |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address when run directly |
| `LOG_LEVEL` | `INFO` | Logging level |

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # optional, LocalStack
uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs: <http://127.0.0.1:8000/docs>

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness plus dependency reachability |
| POST | `/posts` | Create a post (`X-Author` header optional) |
| GET | `/posts` | List posts (`limit`, `next_token`, `status`) |
| GET | `/posts/{post_id}` | Read a post |
| PUT | `/posts/{post_id}` | Update title/body/tags/status |
| DELETE | `/posts/{post_id}` | Delete a post item |
| POST | `/posts/{post_id}/images` | Upload an image (multipart or raw body + `?filename=`) |
| GET | `/posts/{post_id}/images` | List images with presigned GET URLs |
| POST | `/posts/{post_id}/comments` | Submit a comment → SQS, `pending_moderation` |
| GET | `/posts/{post_id}/comments` | List approved comments |
| GET | `/moderation/comments` | Poll pending comments + receipt handles |
| POST | `/moderation/comments/approve` | Publish a comment and delete the SQS message |
| POST | `/moderation/comments/reject` | Discard a comment and delete the SQS message |

Image uploads accept `multipart/form-data` (parsed with the standard library) or
a raw binary body whose name is taken from `?filename=` or the `X-Filename`
header.

## Tests

```bash
python -m pytest -q
```

Tests are fully offline: the storage aggregate is replaced through FastAPI
dependency overrides and boto3 is monkeypatched for the storage-layer tests.
