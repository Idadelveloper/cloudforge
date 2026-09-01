"""AWS data-access layer (DynamoDB, SQS and S3) behind small interfaces.

Every client honours ``AWS_ENDPOINT_URL`` so the same code runs against
LocalStack and real AWS. Resource names are configurable through the
environment with defaults matching the infrastructure plan.
"""

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger("async_job_processor.storage")

DEFAULT_TABLE_NAME = "jobs"
DEFAULT_STATUS_INDEX = "status-created_at-index"
DEFAULT_QUEUE_NAME = "job-queue"
DEFAULT_DLQ_NAME = "job-dlq"
DEFAULT_RESULTS_BUCKET = "job-results"


class JobNotFoundError(Exception):
    """Raised when an update targets a job that does not exist."""


class JobStateConflictError(Exception):
    """Raised when an update fails because the job is in an unexpected state."""


def utc_now() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def _endpoint_url() -> Optional[str]:
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource():
    """Return a configured DynamoDB resource."""
    return boto3.resource("dynamodb", region_name=_region(), endpoint_url=_endpoint_url())


def sqs_client():
    """Return a configured SQS client."""
    return boto3.client("sqs", region_name=_region(), endpoint_url=_endpoint_url())


def s3_client():
    """Return a configured S3 client."""
    return boto3.client("s3", region_name=_region(), endpoint_url=_endpoint_url())


def encode_item(value: Any) -> Any:
    """Convert python values into DynamoDB friendly values (floats -> Decimal)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: encode_item(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_item(val) for val in value]
    return value


def decode_item(value: Any) -> Any:
    """Convert DynamoDB values back into plain JSON friendly python values."""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: decode_item(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [decode_item(val) for val in value]
    if isinstance(value, set):
        return [decode_item(val) for val in value]
    return value


class DynamoJobRepository:
    """DynamoDB backed persistence for job records."""

    def __init__(self, table=None, table_name: Optional[str] = None, status_index: Optional[str] = None):
        self.table_name = table_name or os.environ.get("JOBS_TABLE", DEFAULT_TABLE_NAME)
        self.status_index = status_index or os.environ.get("JOBS_STATUS_INDEX", DEFAULT_STATUS_INDEX)
        self._table = table

    @property
    def table(self):
        """Lazily create the boto3 Table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    def create_job(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a brand new job record."""
        self.table.put_item(Item=encode_item(item))
        return dict(item)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return a job record or ``None`` when unknown."""
        response = self.table.get_item(Key={"job_id": job_id})
        item = response.get("Item")
        if not item:
            return None
        return decode_item(item)

    def update_job(
        self,
        job_id: str,
        updates: Dict[str, Any],
        expected_statuses: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Apply a partial update to a job record and return the new record."""
        clean = {key: val for key, val in updates.items() if val is not None and key != "job_id"}
        if not clean:
            current = self.get_job(job_id)
            if current is None:
                raise JobNotFoundError(job_id)
            return current

        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments: List[str] = []
        for index, key in enumerate(sorted(clean)):
            names["#f%d" % index] = key
            values[":v%d" % index] = clean[key]
            assignments.append("#f%d = :v%d" % (index, index))

        condition = "attribute_exists(job_id)"
        if expected_statuses:
            names["#expected_status"] = "status"
            placeholders = []
            for index, status in enumerate(expected_statuses):
                placeholder = ":s%d" % index
                values[placeholder] = status
                placeholders.append(placeholder)
            condition = "%s AND #expected_status IN (%s)" % (condition, ", ".join(placeholders))

        try:
            response = self.table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=encode_item(values),
                ConditionExpression=condition,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                if self.get_job(job_id) is None:
                    raise JobNotFoundError(job_id) from exc
                raise JobStateConflictError(job_id) from exc
            raise
        return decode_item(response.get("Attributes", {}))

    def list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 25,
        start_key: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """List jobs, optionally filtered by status via the status index."""
        kwargs: Dict[str, Any] = {"Limit": limit}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        if status:
            kwargs["IndexName"] = self.status_index
            kwargs["KeyConditionExpression"] = Key("status").eq(status)
            kwargs["ScanIndexForward"] = False
            response = self.table.query(**kwargs)
        else:
            response = self.table.scan(**kwargs)
        items = [decode_item(item) for item in response.get("Items", [])]
        return items, response.get("LastEvaluatedKey")

    def ping(self) -> bool:
        """Verify the table is reachable."""
        self.table.load()
        return True


class SqsJobQueue:
    """SQS access for publishing jobs and inspecting the dead-letter queue."""

    def __init__(self, client=None, queue_url: Optional[str] = None, dlq_url: Optional[str] = None):
        self._client = client
        self._queue_url = queue_url
        self._dlq_url = dlq_url
        self.queue_name = os.environ.get("JOB_QUEUE_NAME", DEFAULT_QUEUE_NAME)
        self.dlq_name = os.environ.get("JOB_DLQ_NAME", DEFAULT_DLQ_NAME)

    @property
    def client(self):
        """Lazily create the boto3 SQS client."""
        if self._client is None:
            self._client = sqs_client()
        return self._client

    def _lookup_url(self, name: str) -> str:
        return self.client.get_queue_url(QueueName=name)["QueueUrl"]

    @property
    def queue_url(self) -> str:
        """URL of the main job queue."""
        if self._queue_url is None:
            self._queue_url = os.environ.get("JOB_QUEUE_URL") or self._lookup_url(self.queue_name)
        return self._queue_url

    @property
    def dlq_url(self) -> str:
        """URL of the dead-letter queue."""
        if self._dlq_url is None:
            self._dlq_url = os.environ.get("JOB_DLQ_URL") or self._lookup_url(self.dlq_name)
        return self._dlq_url

    def send_job(self, message: Dict[str, Any]) -> str:
        """Publish a job message and return the SQS message id."""
        response = self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(message, default=str),
        )
        return response.get("MessageId", "")

    def receive_dead_letters(self, max_messages: int = 10) -> List[Dict[str, Any]]:
        """Peek at dead-letter messages without hiding them from other readers."""
        response = self.client.receive_message(
            QueueUrl=self.dlq_url,
            MaxNumberOfMessages=max(1, min(int(max_messages), 10)),
            VisibilityTimeout=0,
            WaitTimeSeconds=0,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = []
        for raw in response.get("Messages", []):
            body: Any = raw.get("Body")
            job_id = None
            try:
                parsed = json.loads(body) if body else None
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                body = parsed
                job_id = parsed.get("job_id")
            attributes = raw.get("Attributes", {})
            messages.append(
                {
                    "message_id": raw.get("MessageId", ""),
                    "job_id": job_id,
                    "body": body,
                    "approximate_receive_count": int(attributes.get("ApproximateReceiveCount", 0) or 0),
                }
            )
        return messages

    def ping(self) -> bool:
        """Verify the job queue is reachable."""
        self.client.get_queue_attributes(QueueUrl=self.queue_url, AttributeNames=["QueueArn"])
        return True


class S3ResultStore:
    """S3 access for large job result payloads."""

    def __init__(self, client=None, bucket: Optional[str] = None):
        self._client = client
        self.bucket = bucket or os.environ.get("RESULTS_BUCKET", DEFAULT_RESULTS_BUCKET)

    @property
    def client(self):
        """Lazily create the boto3 S3 client."""
        if self._client is None:
            self._client = s3_client()
        return self._client

    def put_json(self, key: str, document: Any) -> str:
        """Store a JSON document and return the object key."""
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(document, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        return key

    def presigned_url(self, key: str, expires_in: Optional[int] = None) -> str:
        """Return a presigned GET url for a stored result object."""
        ttl = expires_in or int(os.environ.get("RESULT_URL_TTL_SECONDS", "3600"))
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl,
        )
