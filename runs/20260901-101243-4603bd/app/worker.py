"""SQS triggered Lambda worker that executes jobs and records the results.

The event source mapping delivers job messages from ``job-queue``. On failure
the handler raises, which makes SQS redeliver the message; the queue redrive
policy (maxReceiveCount=2) allows exactly one retry before the message is
moved to ``job-dlq``. When the final attempt fails the job record is marked
DEAD_LETTER so the API can report it.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from models import (
    STATUS_CANCELLED,
    STATUS_DEAD_LETTER,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
)
from storage import DynamoJobRepository, S3ResultStore, utc_now

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("async_job_processor.worker")

MAX_RECEIVE_COUNT = int(os.environ.get("MAX_RECEIVE_COUNT", "2"))
MAX_INLINE_RESULT_BYTES = int(os.environ.get("MAX_INLINE_RESULT_BYTES", "8192"))

_REPOSITORY: Optional[DynamoJobRepository] = None
_RESULT_STORE: Optional[S3ResultStore] = None


def get_repository() -> DynamoJobRepository:
    """Return the shared DynamoDB repository (created on first use)."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = DynamoJobRepository()
    return _REPOSITORY


def get_result_store() -> S3ResultStore:
    """Return the shared S3 result store (created on first use)."""
    global _RESULT_STORE
    if _RESULT_STORE is None:
        _RESULT_STORE = S3ResultStore()
    return _RESULT_STORE


def _handle_echo(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"echo": payload}


def _numbers(payload: Dict[str, Any], field: str) -> List[float]:
    raw = payload.get(field)
    if not isinstance(raw, list) or not raw:
        raise ValueError("payload.%s must be a non-empty list of numbers" % field)
    values = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, (int, float)):
            raise ValueError("payload.%s must contain numbers only" % field)
        values.append(entry)
    return values


def _handle_sum(payload: Dict[str, Any]) -> Dict[str, Any]:
    values = _numbers(payload, "numbers")
    return {"sum": sum(values), "count": len(values)}


def _handle_multiply(payload: Dict[str, Any]) -> Dict[str, Any]:
    values = _numbers(payload, "factors")
    product: float = 1
    for value in values:
        product = product * value
    return {"product": product, "count": len(values)}


def _handle_word_count(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = payload.get("text")
    if not isinstance(text, str):
        raise ValueError("payload.text must be a string")
    return {"word_count": len(text.split()), "character_count": len(text)}


JOB_HANDLERS = {
    "echo": _handle_echo,
    "sum": _handle_sum,
    "multiply": _handle_multiply,
    "word_count": _handle_word_count,
}


def execute_job(job_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run the compute job and return its result document."""
    handler = JOB_HANDLERS.get(job_type)
    if handler is None:
        raise ValueError("unsupported job_type: %s" % job_type)
    return handler(payload or {})


def process_record(record: Dict[str, Any], repository, result_store) -> str:
    """Process one SQS record; returns an outcome string or raises on failure."""
    body_raw = record.get("body") or record.get("Body") or "{}"
    try:
        body = json.loads(body_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("message body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("message body must be a JSON object")

    job_id = body.get("job_id")
    if not job_id:
        raise ValueError("message body is missing job_id")

    attributes = record.get("attributes") or record.get("Attributes") or {}
    receive_count = int(attributes.get("ApproximateReceiveCount", 1) or 1)

    job = repository.get_job(job_id)
    if job is None:
        logger.warning("job %s not found; dropping message", job_id)
        return "SKIPPED_UNKNOWN"
    if job.get("status") == STATUS_CANCELLED:
        logger.info("job %s was cancelled; skipping", job_id)
        return "SKIPPED_CANCELLED"
    if job.get("status") == STATUS_SUCCEEDED:
        logger.info("job %s already succeeded; skipping", job_id)
        return "SKIPPED_DUPLICATE"

    started = utc_now()
    repository.update_job(
        job_id,
        {
            "status": STATUS_RUNNING,
            "attempts": receive_count,
            "started_at": job.get("started_at") or started,
            "updated_at": started,
        },
    )

    job_type = body.get("job_type") or job.get("job_type") or ""
    payload = body.get("payload")
    if not isinstance(payload, dict):
        payload = job.get("payload") or {}

    try:
        result = execute_job(job_type, payload)
    except Exception as exc:  # noqa: BLE001 - failures drive the retry flow
        final_attempt = receive_count >= MAX_RECEIVE_COUNT
        status = STATUS_DEAD_LETTER if final_attempt else STATUS_FAILED
        now = utc_now()
        updates = {
            "status": status,
            "attempts": receive_count,
            "updated_at": now,
            "error_message": str(exc)[:1000],
        }
        if final_attempt:
            updates["completed_at"] = now
        try:
            repository.update_job(job_id, updates)
        except Exception:  # noqa: BLE001 - best effort bookkeeping
            logger.exception("could not record failure for job %s", job_id)
        logger.error("job %s failed on attempt %d: %s", job_id, receive_count, exc)
        raise

    serialized = json.dumps(result, default=str)
    completed = utc_now()
    updates: Dict[str, Any] = {
        "status": STATUS_SUCCEEDED,
        "attempts": receive_count,
        "updated_at": completed,
        "completed_at": completed,
    }
    if len(serialized.encode("utf-8")) > MAX_INLINE_RESULT_BYTES:
        key = "results/%s.json" % job_id
        result_store.put_json(key, result)
        updates["result_location"] = key
        outcome = "SUCCEEDED_S3"
    else:
        updates["result"] = result
        outcome = "SUCCEEDED"

    repository.update_job(job_id, updates)
    logger.info("job %s completed (%s)", job_id, outcome)
    return outcome


def lambda_handler(event: Optional[Dict[str, Any]], context: Any = None) -> Dict[str, Any]:
    """Entrypoint invoked by the SQS event source mapping."""
    records = (event or {}).get("Records", [])
    repository = get_repository()
    result_store = get_result_store()

    processed: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for record in records:
        message_id = record.get("messageId") or record.get("MessageId", "")
        try:
            outcome = process_record(record, repository, result_store)
            processed.append({"message_id": message_id, "outcome": outcome})
        except Exception as exc:  # noqa: BLE001 - reported after the loop
            failures.append({"message_id": message_id, "error": str(exc)})

    if failures:
        raise RuntimeError(
            "failed to process %d of %d records: %s" % (len(failures), len(records), json.dumps(failures))
        )
    return {"processed": processed, "count": len(processed)}


handler = lambda_handler
