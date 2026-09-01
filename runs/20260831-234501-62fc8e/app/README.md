# image_gallery_backend

FastAPI service that manages photo albums (DynamoDB) and their images (S3).
Clients upload and download the image bytes directly to/from S3 using
presigned URLs issued by this API.

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/health` | Status plus reachability of the bucket and both tables |
| POST | `/albums` | Create an album |
| GET | `/albums` | List albums (`limit`, `next_token`) |
| GET | `/albums/{album_id}` | Fetch one album |
| PATCH | `/albums/{album_id}` | Update title/description |
| DELETE | `/albums/{album_id}` | Cascade delete (S3 objects + image items + album) |
| POST | `/albums/{album_id}/images` | Register a pending image, return presigned PUT URL |
| POST | `/albums/{album_id}/images/{image_id}/complete` | Confirm the upload (head_object) |
| GET | `/albums/{album_id}/images` | List images with presigned GET URLs |
| GET | `/albums/{album_id}/images/{image_id}` | One image + fresh download URL |
| DELETE | `/albums/{album_id}/images/{image_id}` | Delete object + metadata |

## Configuration

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `AWS_ENDPOINT_URL` | *(unset)* | Set to e.g. `http://localhost:4566` for LocalStack |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `GALLERY_BUCKET` | `image-gallery-media` | S3 bucket for image bytes |
| `ALBUMS_TABLE` | `image-gallery-albums` | DynamoDB table (PK `album_id`) |
| `IMAGES_TABLE` | `image-gallery-images` | DynamoDB table (PK `album_id`, SK `image_id`) |
| `PRESIGN_TTL_SECONDS` | `900` | Presigned URL lifetime |
| `APP_CONFIG_SECRET` | *(unset)* | Optional Secrets Manager id overriding the above |
| `CORS_ALLOW_ORIGINS` | `*` | Comma separated allowed origins |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address when run directly |

## Run

```bash
python -m pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # LocalStack (optional)
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs: <http://127.0.0.1:8000/docs>

## Upload flow

1. `POST /albums` → `album_id`
2. `POST /albums/{album_id}/images` with `filename` + `content_type` → `upload_url`
3. `PUT` the raw bytes to `upload_url` (same `Content-Type`)
4. `POST /albums/{album_id}/images/{image_id}/complete` → status becomes `available`

## Tests

```bash
python -m pytest
```

Tests are fully offline: the HTTP layer runs against an injected in-memory
repository and the storage layer against stubbed S3/DynamoDB objects, so no
AWS or LocalStack instance is required.
