"""FastAPI application for the asynchronous job processing service.

Clients submit compute jobs over REST. Job metadata is persisted in DynamoDB
and a message is published to an SQS queue that triggers a Lambda worker
(see ``worker.py``). Clients poll status/result endpoints; failures that
exhaust the single retry land in the dead-letter queue which can be
inspected for operational triage.
"""

import base64
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from models import (
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_SUCCEEDED,
    DeadLetterListResponse,
    JobListResponse,
    JobResultResponse,
    JobStatusResponse,
    JobSubmissionRequest,
    JobSubmissionResponse,
)
from storage import (
    DynamoJobRepository,
    JobNotFoundError,
    JobStateConflictError,
    S3ResultStore,
    SqsJobQueue,
    utc_now,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("async_job_processor")

app = FastAPI(
    title="async_job_processor",
    version="1.0.0",
    description="Asynchronous compute job submission, queueing and result retrieval.",
)

_repository: Optional[DynamoJobRepository] = None
_queue: Optional[SqsJobQueue] = None
_result_store: Optional[S3ResultStore] = None


def get_repository() -> DynamoJobRepository:
    """Return the (lazily built) DynamoDB backed job repository."""
    global _repository
    if _repository is None:
        _repository = DynamoJobRepository()
    return _repository


def get_queue() -> SqsJobQueue:
    """Return the (lazily built) SQS job queue client."""
    global _queue
    if _queue is None:
        _queue = SqsJobQueue()
    return _queue


def get_result_store() -> S3ResultStore:
    """Return the (lazily built) S3 result store client."""
    global _result_store
    if _result_store is None:
        _result_store = S3ResultStore()
    return _result_store


def _encode_token(key: Optional[Dict[str, Any]]) -> Optional[str]:
    if not key:
        return None
    raw = json.dumps(key, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_token(token: str) -> Dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - user supplied token
        raise HTTPException(status_code=400, detail="invalid next_token") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=400, detail="invalid next_token")
    return decoded


@app.get("/health")
def health(repository=Depends(get_repository), queue=Depends(get_queue)) -> JSONResponse:
    """Verify connectivity to DynamoDB and SQS."""
    components: Dict[str, str] = {}
    healthy = True
    for name, check in (("dynamodb", repository.ping), ("sqs", queue.ping)):
        try:
            check()
            components[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - health probe must not raise
            logger.warning("health check failed for %s: %s", name, exc)
            components[name] = "unavailable"
            healthy = False
    body = {"status": "ok" if healthy else "degraded", "components": components}
    return JSONResponse(status_code=200 if healthy else 503, content=body)


@app.post("/jobs", status_code=201, response_model=JobSubmissionResponse)
def submit_job(
    request: JobSubmissionRequest,
    repository=Depends(get_repository),
    queue=Depends(get_queue),
) -> JobSubmissionResponse:
    """Create a job record and enqueue it for asynchronous processing."""
    priority = (request.priority or "normal").lower()
    if priority not in ALLOWED_PRIORITIES:
        raise HTTPException(status_code=400, detail="unsupported priority: %s" % priority)

    now = utc_now()
    job_id = str(uuid.uuid4())
    item: Dict[str, Any] = {
        "job_id": job_id,
        "job_type": request.job_type,
        "payload": request.payload,
        "priority": priority,
        "status": STATUS_QUEUED,
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }
    if request.callback_metadata:
        item["callback_metadata"] = request.callback_metadata

    try:
        repository.create_job(item)
    except Exception as exc:  # noqa: BLE001 - surface as 503
        logger.exception("failed to persist job record")
        raise HTTPException(status_code=503, detail="job store unavailable") from exc

    message = {
        "job_id": job_id,
        "job_type": request.job_type,
        "payload": request.payload,
        "priority": priority,
        "submitted_at": now,
    }
    try:
        message_id = queue.send_job(message)
    except Exception as exc:  # noqa: BLE001 - surface as 503
        logger.exception("failed to enqueue job %s", job_id)
        try:
            repository.update_job(
                job_id,
                {
                    "status": STATUS_FAILED,
                    "error_message": "failed to enqueue job",
                    "updated_at": utc_now(),
                },
            )
        except Exception:  # noqa: BLE001 - best effort bookkeeping
            logger.exception("failed to mark job %s as FAILED", job_id)
        raise HTTPException(status_code=503, detail="job queue unavailable") from exc

    try:
        repository.update_job(job_id, {"sqs_message_id": message_id, "updated_at": utc_now()})
    except Exception:  # noqa: BLE001 - non fatal
        logger.warning("could not record sqs message id for job %s", job_id)

    return JobSubmissionResponse(job_id=job_id, status=STATUS_QUEUED, created_at=now)


@app.get("/jobs/failed/dead-letter", response_model=DeadLetterListResponse)
def list_dead_letter_messages(
    limit: int = Query(10, ge=1, le=10),
    queue=Depends(get_queue),
) -> DeadLetterListResponse:
    """Peek at the messages currently sitting in the dead-letter queue."""
    try:
        messages = queue.receive_dead_letters(max_messages=limit)
    except Exception as exc:  # noqa: BLE001 - surface as 503
        logger.exception("failed to read dead-letter queue")
        raise HTTPException(status_code=503, detail="dead-letter queue unavailable") from exc
    return DeadLetterListResponse(messages=messages, count=len(messages))


@app.get("/jobs", response_model=JobListResponse)
def list_jobs(
    status: Optional[str] = Query(None, description="Filter by job status"),
    limit: int = Query(25, ge=1, le=100),
    next_token: Optional[str] = Query(None),
    repository=Depends(get_repository),
) -> JobListResponse:
    """List jobs, optionally filtered by status, with pagination."""
    status_filter = None
    if status is not None:
        status_filter = status.upper()
        if status_filter not in ALLOWED_STATUSES:
            raise HTTPException(status_code=400, detail="unsupported status filter: %s" % status)

    start_key = _decode_token(next_token) if next_token else None
    try:
        items, last_key = repository.list_jobs(status=status_filter, limit=limit, start_key=start_key)
    except Exception as exc:  # noqa: BLE001 - surface as 503
        logger.exception("failed to list jobs")
        raise HTTPException(status_code=503, detail="job store unavailable") from exc

    return JobListResponse(jobs=items, count=len(items), next_token=_encode_token(last_key))


@app.get("/jobs/{job_id}")
def get_job(job_id: str, repository=Depends(get_repository)) -> Dict[str, Any]:
    """Return the full job record."""
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found: %s" % job_id)
    return job


@app.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
def get_job_status(job_id: str, repository=Depends(get_repository)) -> JobStatusResponse:
    """Lightweight polling endpoint for job status."""
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found: %s" % job_id)
    return JobStatusResponse(
        job_id=job_id,
        status=job.get("status", STATUS_QUEUED),
        attempts=int(job.get("attempts") or 0),
        created_at=job.get("created_at", ""),
        updated_at=job.get("updated_at", ""),
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
        error_message=job.get("error_message"),
    )


@app.get("/jobs/{job_id}/result", response_model=JobResultResponse)
def get_job_result(
    job_id: str,
    repository=Depends(get_repository),
    result_store=Depends(get_result_store),
) -> JobResultResponse:
    """Return the job result once the job has succeeded."""
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found: %s" % job_id)

    status = job.get("status", STATUS_QUEUED)
    if status != STATUS_SUCCEEDED:
        raise HTTPException(status_code=409, detail="job result not available (status=%s)" % status)

    result_url = None
    location = job.get("result_location")
    if location:
        try:
            result_url = result_store.presigned_url(location)
        except Exception as exc:  # noqa: BLE001 - surface as 503
            logger.exception("failed to presign result for job %s", job_id)
            raise HTTPException(status_code=503, detail="result storage unavailable") from exc

    return JobResultResponse(
        job_id=job_id,
        status=status,
        result=job.get("result"),
        result_url=result_url,
        completed_at=job.get("completed_at"),
    )


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str, repository=Depends(get_repository)) -> Dict[str, Any]:
    """Cancel a job that has not started processing yet."""
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found: %s" % job_id)

    status = job.get("status", STATUS_QUEUED)
    if status == STATUS_CANCELLED:
        return job
    if status != STATUS_QUEUED:
        raise HTTPException(status_code=409, detail="cannot cancel job in status %s" % status)

    now = utc_now()
    updates = {"status": STATUS_CANCELLED, "updated_at": now, "completed_at": now}
    try:
        return repository.update_job(job_id, updates, expected_statuses=[STATUS_QUEUED])
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="job not found: %s" % job_id) from exc
    except JobStateConflictError as exc:
        raise HTTPException(status_code=409, detail="job already started processing") from exc


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
