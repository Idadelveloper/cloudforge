"""FastAPI entrypoint for the asynchronous job processing service.

The API accepts compute jobs, stores a job record in DynamoDB with status
QUEUED and enqueues an SQS message that is consumed by the Lambda worker
(see ``worker.py``).  Clients poll the status/result endpoints.
"""

import logging
import os
from typing import Any, Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query

from models import (
    DeadLetterResponse,
    HealthResponse,
    JobListResponse,
    JobResultResponse,
    JobStatusResponse,
    JobSubmitRequest,
    JobSubmitResponse,
)
from storage import (
    DynamoJobRepository,
    JobQueue,
    JobRepository,
    SqsJobQueue,
    utc_now,
)
from tasks import SUPPORTED_JOB_TYPES

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("async_job_processor.api")

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_DEAD_LETTER = "DEAD_LETTER"

ALLOWED_STATUSES = {
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_DEAD_LETTER,
}
PENDING_STATUSES = {STATUS_QUEUED, STATUS_RUNNING}
RETRYABLE_STATUSES = {STATUS_FAILED, STATUS_DEAD_LETTER}

app = FastAPI(
    title="async_job_processor",
    version="1.0.0",
    description="Submit compute jobs, process them asynchronously and poll for results.",
)

_repository: Optional[JobRepository] = None
_queue: Optional[JobQueue] = None


def get_repository() -> JobRepository:
    """Return the (lazily created) job repository."""
    global _repository
    if _repository is None:
        _repository = DynamoJobRepository()
    return _repository


def get_queue() -> JobQueue:
    """Return the (lazily created) job queue client."""
    global _queue
    if _queue is None:
        _queue = SqsJobQueue()
    return _queue


def _safe_ping(dependency: Any) -> bool:
    try:
        return bool(dependency.ping())
    except Exception as exc:
        LOGGER.warning("dependency ping failed: %s", exc)
        return False


def _require_job(repo: JobRepository, job_id: str) -> dict:
    try:
        job = repo.get_job(job_id)
    except Exception as exc:
        LOGGER.error("failed to read job %s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail="job store unavailable")
    if not job:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    return job


@app.post("/jobs", response_model=JobSubmitResponse, status_code=201)
def submit_job(
    request: JobSubmitRequest,
    repo: JobRepository = Depends(get_repository),
    queue: JobQueue = Depends(get_queue),
) -> JobSubmitResponse:
    """Create a job record and enqueue it for asynchronous processing."""
    if request.job_type not in SUPPORTED_JOB_TYPES:
        raise HTTPException(
            status_code=400,
            detail="unsupported job_type '%s'; supported: %s"
            % (request.job_type, ", ".join(sorted(SUPPORTED_JOB_TYPES))),
        )

    job_id = str(uuid4())
    now = utc_now()
    record = {
        "job_id": job_id,
        "job_type": request.job_type,
        "payload": request.payload,
        "priority": request.priority or "normal",
        "status": STATUS_QUEUED,
        "attempts": 0,
        "result": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }

    try:
        repo.create_job(record)
    except Exception as exc:
        LOGGER.error("failed to persist job %s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail="job store unavailable")

    message = {
        "job_id": job_id,
        "job_type": request.job_type,
        "payload": request.payload,
        "submitted_at": now,
    }
    try:
        queue.send_job(message)
    except Exception as exc:
        LOGGER.error("failed to enqueue job %s: %s", job_id, exc)
        failure_time = utc_now()
        try:
            repo.update_job(
                job_id,
                {
                    "status": STATUS_FAILED,
                    "error": "failed to enqueue job: %s" % exc,
                    "updated_at": failure_time,
                    "completed_at": failure_time,
                },
            )
        except Exception as update_exc:
            LOGGER.error("failed to mark job %s as FAILED: %s", job_id, update_exc)
        raise HTTPException(status_code=503, detail="failed to enqueue job")

    return JobSubmitResponse(job_id=job_id, status=STATUS_QUEUED, created_at=now)


@app.get("/jobs/dead-letter", response_model=DeadLetterResponse)
def peek_dead_letter(
    max_messages: int = Query(default=10, ge=1, le=10),
    queue: JobQueue = Depends(get_queue),
) -> DeadLetterResponse:
    """Peek (without deleting) messages sitting in the dead-letter queue."""
    try:
        messages = queue.peek_dead_letter(max_messages=max_messages)
    except Exception as exc:
        LOGGER.error("failed to peek dead-letter queue: %s", exc)
        raise HTTPException(status_code=503, detail="dead-letter queue unavailable")
    return DeadLetterResponse(messages=messages, count=len(messages))


@app.get("/jobs", response_model=JobListResponse)
def list_jobs(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=25, ge=1, le=100),
    next_token: Optional[str] = Query(default=None),
    repo: JobRepository = Depends(get_repository),
) -> JobListResponse:
    """List jobs, optionally filtered by status, with opaque pagination."""
    normalized = None
    if status_filter is not None:
        normalized = status_filter.upper()
        if normalized not in ALLOWED_STATUSES:
            raise HTTPException(
                status_code=400,
                detail="invalid status '%s'; allowed: %s"
                % (status_filter, ", ".join(sorted(ALLOWED_STATUSES))),
            )
    try:
        items, token = repo.list_jobs(status=normalized, limit=limit, next_token=next_token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        LOGGER.error("failed to list jobs: %s", exc)
        raise HTTPException(status_code=503, detail="job store unavailable")
    return JobListResponse(items=items, next_token=token, count=len(items))


@app.get("/jobs/{job_id}")
def get_job(job_id: str, repo: JobRepository = Depends(get_repository)) -> dict:
    """Return the full job record."""
    return _require_job(repo, job_id)


@app.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
def get_job_status(job_id: str, repo: JobRepository = Depends(get_repository)) -> JobStatusResponse:
    """Lightweight status poll."""
    job = _require_job(repo, job_id)
    return JobStatusResponse(
        job_id=job.get("job_id", job_id),
        status=job.get("status", "UNKNOWN"),
        attempts=int(job.get("attempts") or 0),
        updated_at=job.get("updated_at"),
    )


@app.get("/jobs/{job_id}/result", response_model=JobResultResponse)
def get_job_result(job_id: str, repo: JobRepository = Depends(get_repository)) -> JobResultResponse:
    """Return the computed result of a finished job."""
    job = _require_job(repo, job_id)
    job_status = job.get("status", "UNKNOWN")
    if job_status in PENDING_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="job '%s' is not finished yet (status=%s)" % (job_id, job_status),
        )
    return JobResultResponse(
        job_id=job.get("job_id", job_id),
        status=job_status,
        result=job.get("result"),
        error=job.get("error"),
        completed_at=job.get("completed_at"),
    )


@app.post("/jobs/{job_id}/retry", response_model=JobStatusResponse)
def retry_job(
    job_id: str,
    repo: JobRepository = Depends(get_repository),
    queue: JobQueue = Depends(get_queue),
) -> JobStatusResponse:
    """Manually requeue a FAILED or dead-lettered job."""
    job = _require_job(repo, job_id)
    job_status = job.get("status", "UNKNOWN")
    if job_status not in RETRYABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="job '%s' cannot be retried from status %s" % (job_id, job_status),
        )

    now = utc_now()
    updates = {
        "status": STATUS_QUEUED,
        "error": None,
        "result": None,
        "updated_at": now,
        "completed_at": None,
    }
    try:
        updated = repo.update_job(job_id, updates) or {}
    except Exception as exc:
        LOGGER.error("failed to reset job %s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail="job store unavailable")

    try:
        queue.send_job(
            {
                "job_id": job_id,
                "job_type": job.get("job_type", ""),
                "payload": job.get("payload") or {},
                "submitted_at": now,
            }
        )
    except Exception as exc:
        LOGGER.error("failed to requeue job %s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail="failed to enqueue job")

    return JobStatusResponse(
        job_id=job_id,
        status=STATUS_QUEUED,
        attempts=int(updated.get("attempts") or job.get("attempts") or 0),
        updated_at=now,
    )


@app.get("/healthz", response_model=HealthResponse)
def healthz(
    repo: JobRepository = Depends(get_repository),
    queue: JobQueue = Depends(get_queue),
) -> HealthResponse:
    """Report liveness plus reachability of DynamoDB and SQS."""
    table_ok = _safe_ping(repo)
    queue_ok = _safe_ping(queue)
    return HealthResponse(
        status="ok" if table_ok and queue_ok else "degraded",
        table=table_ok,
        queue=queue_ok,
        table_name=str(getattr(repo, "table_name", "unknown")),
        queue_name=str(getattr(queue, "queue_name", "unknown")),
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
