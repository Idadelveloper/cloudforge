# async_job_processor

Asynchronous compute-job processing service.

* `POST /jobs` writes a job record to DynamoDB (`jobs`) with status `QUEUED` and
  sends the payload to the SQS `job-queue`.
* `worker.lambda_handler` is the SQS-triggered Lambda that executes the job and
  writes the result to the `job-results` table (large results overflow to S3).
* Failed messages are retried once (`maxReceiveCount = 2`) and then routed to
  the `job-dlq` dead-letter queue, which can be inspected and replayed.

## Endpoints

| Method | Path                            | Purpose                                        |
| ------ | ------------------------------- | ---------------------------------------------- |
| POST   | `/jobs`                         | Submit a job (supports `idempotency_key`)      |
| GET    | `/jobs`                         | List jobs (`status`, `limit`, `cursor`)        |
| GET    | `/jobs/{job_id}`                | Full job record                                |
| GET    | `/jobs/{job_id}/status`         | Lightweight status poll                        |
| GET    | `/jobs/{job_id}/result`         | Result (inline or presigned S3 URL)            |
| DELETE | `/jobs/{job_id}`                | Cancel a queued job / delete a terminal record |
| GET    | `/dead-letters`                 | Peek at dead-lettered messages                 |
| POST   | `/dead-letters/{job_id}/replay` | Re-enqueue a dead-lettered job                 |
| GET    | `/healthz`                      | DynamoDB + SQS reachability                    |

Supported `job_type` values: `echo`, `sum`, `uppercase`, `wordcount`.

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # LocalStack (optional)
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 127.0.0.1 --port 8000
```

## Configuration (environment variables)

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `AWS_ENDPOINT_URL` | *(unset)* | Endpoint for all AWS clients (LocalStack) |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `JOBS_TABLE` | `jobs` | Job records table |
| `RESULTS_TABLE` | `job-results` | Job results table |
| `JOBS_STATUS_INDEX` | `status-created_at-index` | GSI used by `GET /jobs?status=` |
| `JOB_QUEUE_NAME` / `JOB_QUEUE_URL` | `job-queue` | Main work queue |
| `JOB_DLQ_NAME` / `JOB_DLQ_URL` | `job-dlq` | Dead-letter queue |
| `RESULTS_BUCKET` | `job-results-bucket` | Overflow bucket for large results |
| `JOB_FAILURE_TOPIC_ARN` | *(unset)* | SNS topic for dead-letter alerts |
| `API_CONFIG_SECRET` | `job-api-config` | Secrets Manager secret holding the API token |
| `API_AUTH_TOKEN` | *(unset)* | Overrides the Secrets Manager lookup |
| `JOB_MAX_ATTEMPTS` | `2` | Initial attempt + one retry |
| `INLINE_RESULT_LIMIT_BYTES` | `307200` | Above this a result goes to S3 |

When neither `API_AUTH_TOKEN` nor the secret is available, authentication is
disabled so the service still runs locally. When a credential is configured,
send it as `Authorization: Bearer <credential>`.

## Tests

```bash
python -m pytest
```

Tests are fully offline: the repository and the API credential provider are
replaced with in-memory fakes/stubs, so no AWS or LocalStack endpoint is
required. (The FastAPI test client needs `httpx`, a Starlette test dependency.)
