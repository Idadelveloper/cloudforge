# Image Gallery Backend

FastAPI JSON backend for an image gallery.

* Album metadata lives in the DynamoDB table `albums` (partition key `album_id`).
* Image metadata lives in the DynamoDB table `images` (partition key `album_id`, sort key `image_id`).
* Image binaries live in the S3 bucket `image-gallery-media`; clients upload the bytes directly to S3
  using short-lived presigned `PUT` URLs and view them through presigned `GET` URLs.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AWS_ENDPOINT_URL` | *(unset)* | Endpoint override for LocalStack (e.g. `http://localhost:4566`) |
| `AWS_DEFAULT_REGION` | `us-east-1` | Region for all AWS clients |
| `S3_BUCKET` | `image-gallery-media` | Media bucket |
| `ALBUMS_TABLE` | `albums` | Album metadata table |
| `IMAGES_TABLE` | `images` | Image metadata table |
| `PRESIGN_EXPIRES_SECONDS` | `900` | Lifetime of presigned URLs |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address of the dev server |
| `LOG_LEVEL` | `INFO` | Root log level |

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# against LocalStack
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test

uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs: <http://127.0.0.1:8000/docs>

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Reports S3 bucket and DynamoDB table reachability (503 when degraded) |
| POST | `/albums` | Create an album |
| GET | `/albums` | List albums (`limit`, `next_token`) |
| GET | `/albums/{album_id}` | Fetch one album |
| PATCH | `/albums/{album_id}` | Update title/description |
| DELETE | `/albums/{album_id}` | Delete album, its image records and its S3 objects |
| POST | `/albums/{album_id}/images` | Register a pending image, returns presigned PUT URL |
| POST | `/albums/{album_id}/images/{image_id}/complete` | Confirm upload, records size/content-type |
| GET | `/albums/{album_id}/images` | List images with presigned GET URLs |
| GET | `/albums/{album_id}/images/{image_id}` | Fetch one image with a presigned GET URL |
| DELETE | `/albums/{album_id}/images/{image_id}` | Delete the S3 object and its metadata |

### Upload flow

1. `POST /albums/{album_id}/images` with `{"filename": "beach.jpg", "content_type": "image/jpeg"}`.
2. `PUT` the raw bytes to the returned `upload_url`, sending the `required_headers` (`Content-Type`).
3. `POST /albums/{album_id}/images/{image_id}/complete` to flip the status to `available`.

## Tests

```bash
pip install pytest
pytest
```

The test suite is fully offline: the HTTP layer runs against an in-memory fake repository injected via
`app.dependency_overrides`, and the storage layer is exercised with lightweight boto3 stubs.
