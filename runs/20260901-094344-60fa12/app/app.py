"""FastAPI application for the asynchronous job-processing service.

Clients submit compute jobs, the API records them in DynamoDB and enqueues them
on SQS. A Lambda worker (see ``worker.py``) executes the jobs and writes results
back to DynamoDB/S3. Clients poll for status and fetch results here.
"""
from __future__ import annotations

import hmac
import os
import uuid
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

import storage
from storage import (
    STATUS_CANCELED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
    VALID_STATUSES,
    utcnow,
)

app = FastAPI(
    title="async_job_processor",
    version="1.0.0",
    description="Submit compute jobs, poll their status and fetch results.",
)


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
def get_repository() -> storage.JobRepository:
    """Return the shared AWS-backed repository (overridable in tests)."""
    return storage.get_repository()


def get_api_token() -> Optional[str]:
    """Return the configured API credential; ``None`` disables authentication."""
    return storage.get_api_token()


def require_auth(
    authorization: Optional[str] = Header(default=None),
    expected: Optional[str] = Depends(get_api_token),
) -> None:
    """Validate the bearer credential when one is configured."""
    if not expected:
        return
    supplied = ""
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            supplied = parts[1].strip()
        else:
            supplied = authorization.strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid or missing API credentials")


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class JobSubmitRequest(BaseModel):
    job_type: str = Field(..., min_length=1, max_length=64)
    payload: Dict[str, Any] = Field(...)
    priority: Optional[str] = Field(default=None, max_length=32)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    attempts: int = 0
    max_attempts: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    error_message: Optional[str] = None


class DeadLetterEntry(BaseModel):
    job_id: str = ""
    message_id: str = ""
    receipt_handle: str = ""
    body: str = ""
    approximate_receive_count: int = 0
    first_seen_at: Optional[str] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _job_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(job)
    payload["attempts"] = int(payload.get("attempts", 0) or 0)
    payload["max_attempts"] = int(payload.get("max_attempts", 0) or 0)
    return payload


def _load_job(repo: storage.JobRepository, job_id: str) -> Dict[str, Any]:
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.post("/jobs", status_code=201, dependencies=[Depends(require_auth)])
def submit_job(
    request: JobSubmitRequest,
    response: Response,
    repo: storage.JobRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Create a job record and enqueue it for asynchronous processing."""
    if request.idempotency_key:
        existing = repo.find_by_idempotency_key(request.idempotency_key)
        if existing:
            response.status_code = 200
            return _job_payload(existing)

    now = utcnow()
    job: Dict[str, Any] = {
        "job_id": str(uuid.uuid4()),
        "job_type": request.job_type,
        "payload": request.payload,
        "status": STATUS_QUEUED,
        "attempts": 0,
        "max_attempts": int(repo.settings.max_attempts),
        "created_at": now,
        "updated_at": now,
        "priority": request.priority or "normal",
    }
    if request.idempotency_key:
        job["idempotency_key"] = request.idempotency_key

    repo.create_job(job)
    try:
        message_id = repo.enqueue_job(job)
    except Exception as exc:
        repo.update_job(
            job["job_id"],
            {
                "status": STATUS_FAILED,
                "error_message": "failed to enqueue job: {0}".format(exc),
                "updated_at": utcnow(),
            },
        )
        raise HTTPException(status_code=502, detail="failed to enqueue job") from exc

    if message_id:
        job = repo.update_job(job["job_id"], {"sqs_message_id": message_id, "updated_at": utcnow()})
    return _job_payload(job)


@app.get("/jobs", dependencies=[Depends(require_auth)])
def list_jobs(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
    repo: storage.JobRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List job records, optionally filtered by status."""
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="status must be one of {0}".format(sorted(VALID_STATUSES)))
    try:
        items, next_cursor = repo.list_jobs(status=status, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "items": [_job_payload(item) for item in items],
        "count": len(items),
        "next_cursor": next_cursor,
    }


@app.get("/jobs/{job_id}", dependencies=[Depends(require_auth)])
def get_job(job_id: str, repo: storage.JobRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Return the full job record."""
    return _job_payload(_load_job(repo, job_id))


@app.get("/jobs/{job_id}/status", response_model=JobStatusResponse, dependencies=[Depends(require_auth)])
def get_job_status(job_id: str, repo: storage.JobRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Lightweight polling endpoint returning only status information."""
    job = _load_job(repo, job_id)
    return {
        "job_id": job.get("job_id", job_id),
        "status": job.get("status", "UNKNOWN"),
        "attempts": int(job.get("attempts", 0) or 0),
        "max_attempts": int(job.get("max_attempts", 0) or 0),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "error_message": job.get("error_message"),
    }


@app.get("/jobs/{job_id}/result", dependencies=[Depends(require_auth)])
def get_job_result(job_id: str, repo: storage.JobRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Return the result of a successfully completed job."""
    job = _load_job(repo, job_id)
    current = job.get("status")
    if current != STATUS_SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail="job is not complete (status={0})".format(current),
        )
    record = repo.get_result(job_id)
    if not record:
        raise HTTPException(status_code=409, detail="result is not available yet")

    body: Dict[str, Any] = {
        "job_id": job_id,
        "status": current,
        "completed_at": record.get("completed_at") or job.get("completed_at"),
        "duration_ms": int(record.get("duration_ms", 0) or 0),
        "result_size_bytes": int(record.get("result_size_bytes", 0) or 0),
    }
    s3_key = record.get("result_s3_key")
    if s3_key:
        body["result_s3_key"] = s3_key
        body["result_url"] = repo.presigned_result_url(s3_key)
    else:
        body["result"] = record.get("result")
    return body


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_auth)])
def delete_job(job_id: str, repo: storage.JobRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Cancel a queued job or delete a terminal job record."""
    job = _load_job(repo, job_id)
    current = job.get("status")
    if current == STATUS_QUEUED:
        now = utcnow()
        updated = repo.update_job(job_id, {"status": STATUS_CANCELED, "updated_at": now, "completed_at": now})
        return {"job_id": job_id, "status": updated.get("status", STATUS_CANCELED), "deleted": False}
    if current == STATUS_RUNNING:
        raise HTTPException(status_code=409, detail="job is running and cannot be canceled")
    if current in TERMINAL_STATUSES:
        repo.delete_job(job_id)
        return {"job_id": job_id, "status": current, "deleted": True}
    raise HTTPException(status_code=409, detail="job in status {0} cannot be deleted".format(current))


@app.get("/dead-letters", response_model=List[DeadLetterEntry], dependencies=[Depends(require_auth)])
def list_dead_letters(
    max_messages: int = Query(default=10, ge=1, le=10),
    repo: storage.JobRepository = Depends(get_repository),
) -> List[Dict[str, Any]]:
    """Peek at messages currently sitting in the dead-letter queue."""
    return repo.receive_dead_letters(max_messages)


@app.post("/dead-letters/{job_id}/replay", dependencies=[Depends(require_auth)])
def replay_dead_letter(job_id: str, repo: storage.JobRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Re-enqueue a dead-lettered job and reset its status to QUEUED."""
    job = _load_job(repo, job_id)

    removed = 0
    for entry in repo.receive_dead_letters(10):
        if entry.get("job_id") == job_id and entry.get("receipt_handle"):
            repo.delete_dead_letter(entry["receipt_handle"])
            removed += 1

    try:
        message_id = repo.enqueue_job(job)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="failed to re-enqueue job") from exc

    now = utcnow()
    updated = repo.update_job(
        job_id,
        {
            "status": STATUS_QUEUED,
            "attempts": 0,
            "updated_at": now,
            "sqs_message_id": message_id,
            "error_message": None,
            "error_type": None,
            "completed_at": None,
        },
    )
    return {
        "job_id": job_id,
        "status": updated.get("status", STATUS_QUEUED),
        "replayed": True,
        "sqs_message_id": message_id,
        "dlq_messages_removed": removed,
    }


@app.get("/healthz")
def healthz(response: Response, repo: storage.JobRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Report reachability of the backing DynamoDB tables and SQS queues."""
    try:
        checks = repo.health()
    except Exception as exc:
        checks = {"dynamodb": False, "sqs": False, "error": str(exc)}
    healthy = bool(checks.get("dynamodb")) and bool(checks.get("sqs"))
    if not healthy:
        response.status_code = 503
    return {"status": "ok" if healthy else "degraded", "checks": checks}


def main() -> None:  # pragma: no cover - convenience entrypoint
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
