"""SQS triggered Lambda worker.

The worker consumes one job message at a time, marks the job RUNNING,
executes the compute task and writes the terminal status back to DynamoDB.
Failures raise so that SQS re-delivers the message; once the receive count
reaches ``MAX_RECEIVE_COUNT`` (2 by default, i.e. one retry) the job record is
marked DEAD_LETTER and the queue redrive policy moves the message to the DLQ.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from storage import DynamoJobRepository, JobRepository, utc_now
from tasks import execute_job

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("async_job_processor.worker")

STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_DEAD_LETTER = "DEAD_LETTER"

_REPOSITORY: Optional[JobRepository] = None


def get_repository() -> JobRepository:
    """Return the (lazily created) DynamoDB repository."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = DynamoJobRepository()
    return _REPOSITORY


def max_receive_count() -> int:
    """Number of deliveries after which a message is dead-lettered."""
    try:
        return max(1, int(os.environ.get("MAX_RECEIVE_COUNT", "2")))
    except (TypeError, ValueError):
        return 2


def process_record(record: Dict[str, Any], repo: JobRepository) -> Dict[str, Any]:
    """Process a single SQS record. Raises on failure."""
    body = record.get("body") or ""
    try:
        message = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ValueError("malformed SQS message body: %s" % exc)
    if not isinstance(message, dict):
        raise ValueError("SQS message body must be a JSON object")

    job_id = message.get("job_id")
    if not job_id:
        raise ValueError("SQS message is missing job_id")

    attributes = record.get("attributes") or {}
    try:
        receive_count = int(attributes.get("ApproximateReceiveCount", 1) or 1)
    except (TypeError, ValueError):
        receive_count = 1

    repo.update_job(
        job_id,
        {
            "status": STATUS_RUNNING,
            "attempts": receive_count,
            "updated_at": utc_now(),
        },
    )

    job_type = message.get("job_type") or ""
    payload = message.get("payload") or {}

    try:
        result = execute_job(job_type, payload)
    except Exception as exc:
        is_final = receive_count >= max_receive_count()
        finished_at = utc_now()
        failure_status = STATUS_DEAD_LETTER if is_final else STATUS_FAILED
        LOGGER.warning(
            "job %s failed on attempt %d (status=%s): %s",
            job_id,
            receive_count,
            failure_status,
            exc,
        )
        repo.update_job(
            job_id,
            {
                "status": failure_status,
                "attempts": receive_count,
                "error": str(exc),
                "updated_at": finished_at,
                "completed_at": finished_at if is_final else None,
            },
        )
        raise

    completed_at = utc_now()
    repo.update_job(
        job_id,
        {
            "status": STATUS_SUCCEEDED,
            "attempts": receive_count,
            "result": result,
            "error": None,
            "updated_at": completed_at,
            "completed_at": completed_at,
        },
    )
    LOGGER.info("job %s succeeded on attempt %d", job_id, receive_count)
    return {"job_id": job_id, "status": STATUS_SUCCEEDED, "attempts": receive_count}


def handler(event: Optional[Dict[str, Any]], context: Any = None, repo: Optional[JobRepository] = None):
    """Lambda entrypoint for the SQS event source."""
    repository = repo or get_repository()
    records = (event or {}).get("Records") or []
    processed: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for record in records:
        try:
            processed.append(process_record(record, repository))
        except Exception as exc:
            failures.append({"message_id": record.get("messageId"), "error": str(exc)})

    if failures:
        raise RuntimeError(
            "failed to process %d record(s): %s" % (len(failures), json.dumps(failures))
        )
    return {"processed": len(processed), "results": processed}


lambda_handler = handler
