"""AWS data-access layer for the asynchronous job-processing service.

All boto3 clients honour the ``AWS_ENDPOINT_URL`` environment variable so the
service works transparently against LocalStack, and default to ``us-east-1``.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_DEAD_LETTER = "DEAD_LETTER"
STATUS_CANCELED = "CANCELED"

VALID_STATUSES = frozenset(
    [
        STATUS_QUEUED,
        STATUS_RUNNING,
        STATUS_SUCCEEDED,
        STATUS_FAILED,
        STATUS_DEAD_LETTER,
        STATUS_CANCELED,
    ]
)

TERMINAL_STATUSES = frozenset(
    [STATUS_SUCCEEDED, STATUS_FAILED, STATUS_DEAD_LETTER, STATUS_CANCELED]
)


def utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


def _endpoint() -> Optional[str]:
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource():
    """Return a DynamoDB resource bound to the configured endpoint/region."""
    return boto3.resource("dynamodb", region_name=_region(), endpoint_url=_endpoint())


def sqs_client():
    """Return an SQS client bound to the configured endpoint/region."""
    return boto3.client("sqs", region_name=_region(), endpoint_url=_endpoint())


def s3_client():
    """Return an S3 client bound to the configured endpoint/region."""
    return boto3.client("s3", region_name=_region(), endpoint_url=_endpoint())


def sns_client():
    """Return an SNS client bound to the configured endpoint/region."""
    return boto3.client("sns", region_name=_region(), endpoint_url=_endpoint())


def secrets_client():
    """Return a Secrets Manager client bound to the configured endpoint/region."""
    return boto3.client("secretsmanager", region_name=_region(), endpoint_url=_endpoint())


def to_dynamo(value: Any) -> Any:
    """Convert a plain Python structure into a DynamoDB-safe structure."""
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def from_dynamo(value: Any) -> Any:
    """Convert DynamoDB Decimals back into plain ints/floats."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, list):
        return [from_dynamo(item) for item in value]
    if isinstance(value, dict):
        return {key: from_dynamo(item) for key, item in value.items()}
    if isinstance(value, set):
        return [from_dynamo(item) for item in value]
    return value


def encode_cursor(last_key: Dict[str, Any]) -> str:
    """Encode a DynamoDB LastEvaluatedKey into an opaque cursor."""
    raw = json.dumps(from_dynamo(last_key), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> Dict[str, Any]:
    """Decode an opaque cursor back into a DynamoDB ExclusiveStartKey."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(decoded, dict):
        raise ValueError("invalid cursor")
    return decoded


class Settings:
    """Environment-driven configuration for AWS resource names."""

    def __init__(self) -> None:
        self.jobs_table = os.environ.get("JOBS_TABLE", "jobs")
        self.results_table = os.environ.get("RESULTS_TABLE", "job-results")
        self.status_index = os.environ.get("JOBS_STATUS_INDEX", "status-created_at-index")
        self.queue_name = os.environ.get("JOB_QUEUE_NAME", "job-queue")
        self.queue_url = os.environ.get("JOB_QUEUE_URL", "")
        self.dlq_name = os.environ.get("JOB_DLQ_NAME", "job-dlq")
        self.dlq_url = os.environ.get("JOB_DLQ_URL", "")
        self.results_bucket = os.environ.get("RESULTS_BUCKET", "job-results-bucket")
        self.failure_topic_arn = os.environ.get("JOB_FAILURE_TOPIC_ARN", "")
        self.secret_name = os.environ.get("API_CONFIG_SECRET", "job-api-config")
        self.max_attempts = int(os.environ.get("JOB_MAX_ATTEMPTS", "2"))
        self.inline_result_limit = int(os.environ.get("INLINE_RESULT_LIMIT_BYTES", str(300 * 1024)))
        self.presign_expiry = int(os.environ.get("RESULT_URL_EXPIRY_SECONDS", "900"))


class JobRepository:
    """Small interface over DynamoDB, SQS, S3 and SNS."""

    def __init__(self, dynamodb=None, sqs=None, s3=None, sns=None, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()
        self._dynamodb = dynamodb
        self._sqs = sqs
        self._s3 = s3
        self._sns = sns
        self._queue_url_cache: Dict[str, str] = {}

    # -- clients ----------------------------------------------------------- #
    def dynamodb(self):
        if self._dynamodb is None:
            self._dynamodb = dynamodb_resource()
        return self._dynamodb

    def sqs(self):
        if self._sqs is None:
            self._sqs = sqs_client()
        return self._sqs

    def s3(self):
        if self._s3 is None:
            self._s3 = s3_client()
        return self._s3

    def sns(self):
        if self._sns is None:
            self._sns = sns_client()
        return self._sns

    def _jobs_table(self):
        return self.dynamodb().Table(self.settings.jobs_table)

    def _results_table(self):
        return self.dynamodb().Table(self.settings.results_table)

    def _resolve_queue_url(self, cache_key: str, configured: str, name: str) -> str:
        if configured:
            return configured
        if cache_key not in self._queue_url_cache:
            response = self.sqs().get_queue_url(QueueName=name)
            self._queue_url_cache[cache_key] = response["QueueUrl"]
        return self._queue_url_cache[cache_key]

    def queue_url(self) -> str:
        return self._resolve_queue_url("main", self.settings.queue_url, self.settings.queue_name)

    def dlq_url(self) -> str:
        return self._resolve_queue_url("dlq", self.settings.dlq_url, self.settings.dlq_name)

    # -- jobs table -------------------------------------------------------- #
    def create_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        self._jobs_table().put_item(
            Item=to_dynamo(job),
            ConditionExpression="attribute_not_exists(job_id)",
        )
        return dict(job)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        response = self._jobs_table().get_item(Key={"job_id": job_id})
        item = response.get("Item")
        return from_dynamo(item) if item else None

    def find_by_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        response = self._jobs_table().scan(
            FilterExpression=Attr("idempotency_key").eq(key),
            Limit=50,
        )
        items = response.get("Items") or []
        return from_dynamo(items[0]) if items else None

    def update_job(self, job_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        set_parts: List[str] = []
        remove_parts: List[str] = []
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        for index, (key, value) in enumerate(updates.items()):
            placeholder = "#k{0}".format(index)
            names[placeholder] = key
            if value is None:
                remove_parts.append(placeholder)
            else:
                set_parts.append("{0} = :v{1}".format(placeholder, index))
                values[":v{0}".format(index)] = to_dynamo(value)

        expression = ""
        if set_parts:
            expression = "SET " + ", ".join(set_parts)
        if remove_parts:
            expression += (" " if expression else "") + "REMOVE " + ", ".join(remove_parts)
        if not expression:
            return self.get_job(job_id) or {"job_id": job_id}

        kwargs: Dict[str, Any] = {
            "Key": {"job_id": job_id},
            "UpdateExpression": expression,
            "ExpressionAttributeNames": names,
            "ReturnValues": "ALL_NEW",
        }
        if values:
            kwargs["ExpressionAttributeValues"] = values
        response = self._jobs_table().update_item(**kwargs)
        return from_dynamo(response.get("Attributes", {}))

    def delete_job(self, job_id: str) -> bool:
        response = self._jobs_table().delete_item(Key={"job_id": job_id}, ReturnValues="ALL_OLD")
        return bool(response.get("Attributes"))

    def list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 25,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        table = self._jobs_table()
        kwargs: Dict[str, Any] = {"Limit": int(limit)}
        if cursor:
            kwargs["ExclusiveStartKey"] = decode_cursor(cursor)
        if status:
            kwargs["IndexName"] = self.settings.status_index
            kwargs["KeyConditionExpression"] = Key("status").eq(status)
            kwargs["ScanIndexForward"] = False
            response = table.query(**kwargs)
        else:
            response = table.scan(**kwargs)
        items = [from_dynamo(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        return items, encode_cursor(last_key) if last_key else None

    # -- results ----------------------------------------------------------- #
    def get_result(self, job_id: str) -> Optional[Dict[str, Any]]:
        response = self._results_table().get_item(Key={"job_id": job_id})
        item = response.get("Item")
        return from_dynamo(item) if item else None

    def put_result(
        self,
        job_id: str,
        result: Optional[Any] = None,
        error_message: Optional[str] = None,
        error_type: Optional[str] = None,
        duration_ms: int = 0,
    ) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "job_id": job_id,
            "duration_ms": int(duration_ms),
            "completed_at": utcnow(),
        }
        if error_message:
            item["error_message"] = str(error_message)[:2000]
        if error_type:
            item["error_type"] = str(error_type)

        size = 0
        if result is not None:
            serialized = json.dumps(result, default=str)
            size = len(serialized.encode("utf-8"))
            if size > self.settings.inline_result_limit:
                key = "results/{0}.json".format(job_id)
                self.s3().put_object(
                    Bucket=self.settings.results_bucket,
                    Key=key,
                    Body=serialized.encode("utf-8"),
                    ContentType="application/json",
                )
                item["result_s3_key"] = key
            else:
                item["result"] = result
        item["result_size_bytes"] = size

        self._results_table().put_item(Item=to_dynamo(item))
        return from_dynamo(to_dynamo(item))

    def presigned_result_url(self, key: str) -> str:
        return self.s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": self.settings.results_bucket, "Key": key},
            ExpiresIn=self.settings.presign_expiry,
        )

    # -- queues ------------------------------------------------------------ #
    def enqueue_job(self, job: Dict[str, Any]) -> str:
        body = json.dumps(
            {
                "job_id": job["job_id"],
                "job_type": job.get("job_type", ""),
                "payload": job.get("payload") or {},
                "priority": job.get("priority") or "normal",
                "enqueued_at": utcnow(),
            },
            default=str,
        )
        response = self.sqs().send_message(QueueUrl=self.queue_url(), MessageBody=body)
        return response.get("MessageId", "")

    def receive_dead_letters(self, max_messages: int = 10) -> List[Dict[str, Any]]:
        response = self.sqs().receive_message(
            QueueUrl=self.dlq_url(),
            MaxNumberOfMessages=max(1, min(10, int(max_messages))),
            VisibilityTimeout=0,
            WaitTimeSeconds=0,
            AttributeNames=["All"],
            MessageAttributeNames=["All"],
        )
        entries: List[Dict[str, Any]] = []
        for message in response.get("Messages", []) or []:
            body = message.get("Body", "") or ""
            job_id = ""
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    job_id = str(parsed.get("job_id", "") or "")
            except ValueError:
                job_id = ""
            attributes = message.get("Attributes", {}) or {}
            entries.append(
                {
                    "job_id": job_id,
                    "message_id": message.get("MessageId", ""),
                    "receipt_handle": message.get("ReceiptHandle", ""),
                    "body": body,
                    "approximate_receive_count": int(attributes.get("ApproximateReceiveCount", 0) or 0),
                    "first_seen_at": _epoch_ms_to_iso(attributes.get("SentTimestamp")),
                }
            )
        return entries

    def delete_dead_letter(self, receipt_handle: str) -> bool:
        self.sqs().delete_message(QueueUrl=self.dlq_url(), ReceiptHandle=receipt_handle)
        return True

    def publish_failure(self, job_id: str, error_message: str) -> Optional[str]:
        arn = self.settings.failure_topic_arn
        if not arn:
            return None
        response = self.sns().publish(
            TopicArn=arn,
            Subject="Job dead-lettered",
            Message=json.dumps({"job_id": job_id, "error": error_message, "at": utcnow()}),
        )
        return response.get("MessageId")

    # -- health ------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        checks: Dict[str, Any] = {"dynamodb": False, "sqs": False}
        try:
            self.dynamodb().meta.client.describe_table(TableName=self.settings.jobs_table)
            checks["dynamodb"] = True
        except Exception as exc:
            checks["dynamodb_error"] = str(exc)
        try:
            self.sqs().get_queue_attributes(
                QueueUrl=self.queue_url(),
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            checks["sqs"] = True
        except Exception as exc:
            checks["sqs_error"] = str(exc)
        return checks


def _epoch_ms_to_iso(raw: Any) -> Optional[str]:
    try:
        millis = int(raw)
    except (TypeError, ValueError):
        return None
    moment = datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc).replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


_REPOSITORY: Optional[JobRepository] = None
_TOKEN_CACHE: Dict[str, Optional[str]] = {}


def get_repository() -> JobRepository:
    """Return a lazily created process-wide repository."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = JobRepository()
    return _REPOSITORY


def reset_repository() -> None:
    """Drop the cached repository (used by tests)."""
    global _REPOSITORY
    _REPOSITORY = None


def reset_token_cache() -> None:
    """Drop the cached API credential (used by tests)."""
    _TOKEN_CACHE.clear()


def _load_token_from_secrets() -> Optional[str]:
    name = Settings().secret_name
    try:
        response = secrets_client().get_secret_value(SecretId=name)
        raw = response.get("SecretString")
    except Exception:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw.strip() or None
    if isinstance(parsed, dict):
        for key in ("api_token", "token", "API_TOKEN", "value"):
            if parsed.get(key):
                return str(parsed[key])
        return None
    return str(parsed)


def get_api_token(force_reload: bool = False) -> Optional[str]:
    """Return the API credential from the environment or Secrets Manager.

    Returns ``None`` when nothing is configured, in which case the API accepts
    unauthenticated requests (useful for local/LocalStack evaluation).
    """
    if not force_reload and "value" in _TOKEN_CACHE:
        return _TOKEN_CACHE["value"]
    value = os.environ.get("API_AUTH_TOKEN") or None
    if value is None:
        value = _load_token_from_secrets()
    _TOKEN_CACHE["value"] = value
    return value
