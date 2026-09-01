# Blog Platform Backend

A FastAPI service for a small blog platform:

* **Posts** (markdown body + metadata) are stored in the DynamoDB table `blog-posts`.
* **Images** are uploaded to the S3 bucket `blog-post-images` and served back through presigned GET URLs.
* **Comments** are never written directly: submissions go onto the SQS queue `blog-comment-moderation`,
  and an approval endpoint copies an approved comment into the DynamoDB table `blog-comments` while
  deleting the queue message. Rejections just delete the message.

## Layout

| file | purpose |
| --- | --- |
| `app.py` | FastAPI routes, request/response models, entrypoint |
| `storage.py` | `BlogRepository` interface + boto3 implementation (DynamoDB / S3 / SQS) |
| `uploads.py` | stdlib multipart / raw body parsing for image uploads |
| `tests/test_app.py` | offline tests using `TestClient` and fake AWS clients |

## Configuration

All settings come from environment variables:

| variable | default |
| --- | --- |
| `AWS_ENDPOINT_URL` | unset (set to e.g. `http://localhost:4566` for LocalStack) |
| `AWS_DEFAULT_REGION` | `us-east-1` |
| `BLOG_POSTS_TABLE` | `blog-posts` |
| `BLOG_COMMENTS_TABLE` | `blog-comments` |
| `BLOG_IMAGES_BUCKET` | `blog-post-images` |
| `BLOG_MODERATION_QUEUE` | `blog-comment-moderation` |
| `BLOG_MODERATION_QUEUE_URL` | resolved via `GetQueueUrl` when unset |
| `BLOG_PRESIGN_EXPIRY` | `3600` (seconds) |
| `BLOG_MAX_IMAGE_BYTES` | `10485760` |
| `HOST` / `PORT` | `127.0.0.1` / `8000` |

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
python app.py                      # or: uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs are available at `http://127.0.0.1:8000/docs`.

## Endpoints

| method | path | description |
| --- | --- | --- |
| POST | `/posts` | create a post (`title`, `body_markdown`, `author`, `tags`, `status`) |
| GET | `/posts` | list post summaries (`limit`, `next_token`, `status`) |
| GET | `/posts/{post_id}` | full post including markdown and image keys |
| PUT | `/posts/{post_id}` | update title / body / author / tags / status |
| DELETE | `/posts/{post_id}` | delete the post and its S3 image objects |
| POST | `/posts/{post_id}/images` | upload an image (multipart form-data or raw body) |
| GET | `/posts/{post_id}/images` | list images with presigned download URLs |
| POST | `/posts/{post_id}/comments` | submit a comment (enqueued for moderation, `202`) |
| GET | `/posts/{post_id}/comments` | list published comments |
| GET | `/moderation/comments` | poll the queue, returns pending comments + receipt handles |
| POST | `/moderation/comments/approve` | publish a comment and delete its message |
| POST | `/moderation/comments/reject` | discard a comment and delete its message |
| GET | `/health` | DynamoDB / S3 / SQS reachability |

Approval expects the pending comment returned by `GET /moderation/comments`:

```bash
curl -X POST localhost:8000/moderation/comments/approve \
  -H 'Content-Type: application/json' \
  -d '{"receipt_handle": "<handle>", "comment_id": "<id>", "moderator": "ada",
       "comment": {"comment_id": "<id>", "post_id": "<post>", "author_name": "bob",
                   "body": "nice post", "submitted_at": "2024-01-01T00:00:00Z"}}'
```

## Tests

```bash
python -m pytest -q
```

The tests inject fake DynamoDB/S3/SQS clients into `AwsBlogRepository`, so they run entirely
offline (no LocalStack, no credentials, no network).
