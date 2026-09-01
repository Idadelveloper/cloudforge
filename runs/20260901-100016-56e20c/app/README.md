# async_job_processor

Asynchronous compute job-processing service.

* `POST /jobs` writes a job record (status `QUEUED`) to DynamoDB and pushes a
  message onto the SQS jobs queue.
* An SQS-triggered Lambda worker (`worker.handler`) executes the compute task
  and writes the terminal status/result back to DynamoDB.
* A failed message is retried once (queue redrive policy `maxReceiveCount = 2`)
  and then lands in the dead-letter queue; the job record is marked
  `FAILED` on the first failure and `DEAD_LETTER` on the final one.

## Endpoints

| Method | Path                   | Purpose                                             |
| ------ | ---------------------- | --------------------------------------------------- |
| POST   | `/jobs`                | Submit a job, returns `job_id` with status `QUEUED`  |
| GET    | `/jobs`                | List jobs (`?status=`, `?limit=`, `?next_token=`)    |
| GET    | `/jobs/dead-letter`    | Peek messages currently in the DLQ                   |
| GET    | `/jobs/{job_id}`       | Full job record                                      |
| GET    | `/jobs/{job_id}/status`| Lightweight status poll                              |
| GET    | `/jobs/{job_id}/result`| Result (409 while `QUEUED`/`RUNNING`)                |
| POST   | `/jobs/{job_id}/retry` | Requeue a `FAILED` / `DEAD_LETTER` job               |
| GET    | `/healthz`             | Liveness + DynamoDB/SQS reachability                 |

Supported `job_type` values: `sum`, `multiply`, `uppercase`, `fibonacci`,
`echo`.

## Configuration

| Variable              | Default                  | Description                          |
| --------------------- | ------------------------ | ------------------------------------ |
| `AWS_ENDPOINT_URL`    | *(unset)*                | Endpoint override, e.g. LocalStack   |
| `AWS_DEFAULT_REGION`  | `us-east-1`              | AWS region                           |
| `JOBS_TABLE`          | `cloudforge-jobs`        | DynamoDB table                       |
| `JOBS_STATUS_INDEX`   | `status-index`           | GSI used by the filtered list        |
| `JOBS_QUEUE_NAME`     | `cloudforge-jobs-queue`  | Main queue name                      |
| `JOBS_DLQ_NAME`       | `cloudforge-jobs-dlq`    | Dead-letter queue name               |
| `JOBS_QUEUE_URL`      | resolved from name       | Explicit queue URL                   |
| `JOBS_DLQ_URL`        | resolved from name       | Explicit DLQ URL                     |
| `MAX_RECEIVE_COUNT`   | `2`                      | Deliveries before dead-lettering     |
| `HOST` / `PORT`       | `127.0.0.1` / `8000`     | Local dev bind address               |

## Run locally

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # when using LocalStack
uvicorn app:app --host 127.0.0.1 --port 8000
```

Submit a job:

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"job_type": "sum", "payload": {"values": [1, 2, 3]}}'
```

## Lambda packaging

The worker entrypoint is `worker.handler` (alias `worker.lambda_handler`); it
only needs `worker.py`, `storage.py` and `tasks.py` plus the boto3 runtime
provided by Lambda.

## Tests

```bash
pip install pytest httpx
pytest
```

All AWS access is faked, so the suite runs completely offline.
