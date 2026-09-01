"""SQS-triggered Lambda worker that executes compute jobs.

The function is wired to the ``job-queue`` event source. Successful jobs write
their result to the ``job-results`` table and flip the job record to SUCCEEDED.
Failed jobs are reported back to SQS via partial batch failures so the message
is retried once (maxReceiveCount = 2) before landing on the dead-letter queue.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

import storage
from storage import (
    STATUS_CANCELED,
    STATUS_DEAD_LETTER,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    utcnow,
)

LOGGER = logging.getLogger("job-worker")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO)


def execute_job(job_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run a short, CPU-light transformation of the submitted payload."""
    kind = (job_type or "").strip().lower()
    payload = payload or {}

    if kind == "echo":
        return {"echo": payload}

    if kind == "sum":
        values = payload.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError("payload.values must be a non-empty list of numbers")
        total = 0.0
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("payload.values must contain only numbers")
            total += float(value)
        return {"sum": total, "count": len(values)}

    if kind == "uppercase":
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("payload.text must be a string")
        return {"text": text.upper(), "length": len(text)}

    if kind == "wordcount":
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("payload.text must be a string")
        words = text.split()
        return {"words": len(words), "characters": len(text)}

    raise ValueError("unsupported job_type: {0}".format(job_type))


def _to_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _handle_failure(repository, job_id, exc, attempts, max_attempts, duration_ms) -> None:
    error_message = str(exc) or exc.__class__.__name__
    error_type = exc.__class__.__name__
    now = utcnow()
    if attempts >= max_attempts:
        repository.update_job(
            job_id,
            {
                "status": STATUS_DEAD_LETTER,
                "attempts": attempts,
                "error_message": error_message,
                "error_type": error_type,
                "completed_at": now,
                "updated_at": now,
            },
        )
        repository.put_result(
            job_id,
            error_message=error_message,
            error_type=error_type,
            duration_ms=duration_ms,
        )
        repository.publish_failure(job_id, error_message)
        LOGGER.error("job %s dead-lettered after %s attempts: %s", job_id, attempts, error_message)
    else:
        repository.update_job(
            job_id,
            {
                "status": STATUS_FAILED,
                "attempts": attempts,
                "error_message": error_message,
                "error_type": error_type,
                "updated_at": now,
            },
        )
        LOGGER.warning("job %s failed on attempt %s, will retry: %s", job_id, attempts, error_message)


def _process_record(record: Dict[str, Any], repository) -> bool:
    """Process a single SQS record. Returns True when the message is done."""
    try:
        body = json.loads(record.get("body") or "{}")
    except ValueError:
        LOGGER.error("dropping unparsable SQS message body")
        return True
    if not isinstance(body, dict):
        LOGGER.error("dropping non-object SQS message body")
        return True

    job_id = str(body.get("job_id") or "")
    if not job_id:
        LOGGER.error("dropping SQS message without job_id")
        return True

    job = repository.get_job(job_id)
    if job is None:
        LOGGER.warning("job %s no longer exists, skipping", job_id)
        return True
    if job.get("status") == STATUS_CANCELED:
        LOGGER.info("job %s was canceled, skipping", job_id)
        return True

    attributes = record.get("attributes") or {}
    attempts = _to_int(attributes.get("ApproximateReceiveCount"), 1)
    max_attempts = _to_int(job.get("max_attempts"), repository.settings.max_attempts)

    now = utcnow()
    repository.update_job(
        job_id,
        {
            "status": STATUS_RUNNING,
            "attempts": attempts,
            "started_at": job.get("started_at") or now,
            "updated_at": now,
        },
    )

    started = time.time()
    try:
        result = execute_job(job.get("job_type", ""), job.get("payload") or body.get("payload") or {})
    except Exception as exc:
        duration_ms = int((time.time() - started) * 1000)
        _handle_failure(repository, job_id, exc, attempts, max_attempts, duration_ms)
        return False

    duration_ms = int((time.time() - started) * 1000)
    repository.put_result(job_id, result=result, duration_ms=duration_ms)
    completed = utcnow()
    repository.update_job(
        job_id,
        {
            "status": STATUS_SUCCEEDED,
            "attempts": attempts,
            "completed_at": completed,
            "updated_at": completed,
            "error_message": None,
            "error_type": None,
        },
    )
    LOGGER.info("job %s succeeded in %sms", job_id, duration_ms)
    return True


def lambda_handler(event: Dict[str, Any], context: Any = None, repo: Any = None) -> Dict[str, Any]:
    """Lambda entrypoint using SQS partial batch responses."""
    repository = repo or storage.get_repository()
    failures: List[Dict[str, str]] = []
    for record in (event or {}).get("Records", []) or []:
        message_id = record.get("messageId", "")
        try:
            done = _process_record(record, repository)
        except Exception as exc:
            LOGGER.error("unhandled worker error for message %s: %s", message_id, exc)
            done = False
        if not done:
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
