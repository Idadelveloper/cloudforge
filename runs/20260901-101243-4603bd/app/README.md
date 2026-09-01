# async_job_processor

Asynchronous job-processing service. Clients submit compute jobs over REST; the
API stores job metadata in DynamoDB and publishes a message to an SQS queue. An
SQS-triggered Lambda worker (`worker.py`) executes the job, writes the result to
DynamoDB (or S3 for large payloads) and updates the status. Failures raise from
the worker so SQS redelivers the message once (`maxReceiveCount=2`); the second
failure sends the message to the dead-letter queue and the job record is marked
`DEAD_LETTER`.

## Layout

| File | Purpose |
| --- | --- |
| `app.py` | FastAPI application and routes |
| `models.py` | Pydantic request/response models and status constants |
| `storage.py` | boto3 data-access layer (DynamoDB / SQS / S3) |
| `worker.py` | Lambda handler consuming the SQS job queue |
| `tests/test_app.py` | Offline tests using `fastapi.testclient.TestClient` |

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| POST | `/jobs` | Submit a job (creates record, enqueues message) |
| GET | `/jobs` | List jobs (`status`, `limit`, `next_token` query params) |
| GET | `/jobs/{job_id}` | Full job record |
| GET | `/jobs/{job_id}/status` | Lightweight status polling |
| GET | `/jobs/{job_id}/result` | Result inline or presigned S3 URL (409 if not done) |
| DELETE | `/jobs/{job_id}` | Cancel a job that is still `QUEUED` |
| GET | `/jobs/failed/dead-letter` | Peek at dead-letter queue messages |
| GET | `/health` | DynamoDB + SQS connectivity probe |

Supported `job_type` values handled by the worker: `echo`, `sum`, `multiply`,
`word_count`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AWS_ENDPOINT_URL` | unset | Set to e.g. `http://localhost:4566` for LocalStack |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `JOBS_TABLE` | `jobs` | DynamoDB table name |
| `JOBS_STATUS_INDEX` | `status-created_at-index` | GSI used for status filtering |
| `JOB_QUEUE_NAME` / `JOB_QUEUE_URL` | `job-queue` | Main SQS queue |
| `JOB_DLQ_NAME` / `JOB_DLQ_URL` | `job-dlq` | Dead-letter queue |
| `RESULTS_BUCKET` | `job-results` | S3 bucket for large results |
| `MAX_RECEIVE_COUNT` | `2` | Attempts before DEAD_LETTER (matches redrive policy) |
| `MAX_INLINE_RESULT_BYTES` | `8192` | Larger results are written to S3 |
| `RESULT_URL_TTL_SECONDS` | `3600` | Presigned URL lifetime |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Local dev bind address |

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 127.0.0.1 --port 8000
```

Submit a job:

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"job_type": "sum", "payload": {"numbers": [1, 2, 3]}}'
```

## Tests

```bash
pytest -q
```

Tests are fully offline: the repository, queue and result store are replaced
with in-memory fakes through FastAPI dependency overrides and monkeypatching,
so no AWS, LocalStack or network access is needed.

## Deploying the worker

Package `worker.py`, `models.py` and `storage.py` and set the Lambda handler to
`worker.lambda_handler`, with the SQS event source mapping bound to the job
queue.
