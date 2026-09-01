"""Data access layer (DynamoDB + SQS) for the async job processing service.

Everything AWS related lives behind the small ``JobRepository`` and
``JobQueue`` interfaces so that the API and the Lambda worker can be tested
with in-memory fakes and run against LocalStack via ``AWS_ENDPOINT_URL``.
"""

import base64
import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key

LOGGER = logging.getLogger("async_job_processor.storage")

DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE_NAME = "cloudforge-jobs"
DEFAULT_QUEUE_NAME = "cloudforge-jobs-queue"
DEFAULT_DLQ_NAME = "cloudforge-jobs-dlq"
STATUS_INDEX = "status-index"


def utc_now() -> str:
    """Current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def aws_region() -> str:
    """Region used for every AWS client."""
    return os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)


def aws_endpoint_url() -> Optional[str]:
    """Optional endpoint override (LocalStack)."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource():
    """Create a DynamoDB resource honouring the endpoint override."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def sqs_client():
    """Create an SQS client honouring the endpoint override."""
    return boto3.client(
        "sqs",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def to_native(value: Any) -> Any:
    """Convert DynamoDB Decimals into plain ints/floats recursively."""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: to_native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_native(item) for item in value]
    if isinstance(value, set):
        return [to_native(item) for item in value]
    return value


def to_dynamo(value: Any) -> Any:
    """Convert a plain Python structure into a DynamoDB-safe structure."""
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def encode_token(last_key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey as an opaque pagination token."""
    if not last_key:
        return None
    raw = json.dumps(to_native(last_key), sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode an opaque pagination token back into a DynamoDB key."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("invalid next_token")
    if not isinstance(decoded, dict):
        raise ValueError("invalid next_token")
    return decoded


class JobRepository(ABC):
    """Persistence interface for job records."""

    @abstractmethod
    def create_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a brand new job record."""

    @abstractmethod
    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return a job record or None."""

    @abstractmethod
    def update_job(self, job_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply a partial update and return the new record."""

    @abstractmethod
    def list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 25,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of job records plus the next pagination token."""

    @abstractmethod
    def ping(self) -> bool:
        """Return True when the backing store is reachable."""


class JobQueue(ABC):
    """Queueing interface for job messages."""

    @abstractmethod
    def send_job(self, message: Dict[str, Any]) -> str:
        """Publish a job message and return the message id."""

    @abstractmethod
    def peek_dead_letter(self, max_messages: int = 10) -> List[Dict[str, Any]]:
        """Return messages currently sitting in the dead-letter queue."""

    @abstractmethod
    def ping(self) -> bool:
        """Return True when the queue is reachable."""


class DynamoJobRepository(JobRepository):
    """DynamoDB backed job repository."""

    def __init__(self, table_name: Optional[str] = None, resource: Any = None) -> None:
        self.table_name = table_name or os.environ.get("JOBS_TABLE", DEFAULT_TABLE_NAME)
        self.index_name = os.environ.get("JOBS_STATUS_INDEX", STATUS_INDEX)
        self._resource = resource
        self._table = None

    @property
    def table(self):
        """Lazily resolve the DynamoDB table object."""
        if self._table is None:
            resource = self._resource or dynamodb_resource()
            self._table = resource.Table(self.table_name)
        return self._table

    def create_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        self.table.put_item(Item=to_dynamo(job))
        return dict(job)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key={"job_id": job_id})
        item = response.get("Item")
        if not item:
            return None
        return to_native(item)

    def update_job(self, job_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not updates:
            return self.get_job(job_id)
        clean = to_dynamo(updates)
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments: List[str] = []
        for index, (key, value) in enumerate(clean.items()):
            names["#k%d" % index] = key
            values[":v%d" % index] = value
            assignments.append("#k%d = :v%d" % (index, index))
        response = self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET " + ", ".join(assignments),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return to_native(response.get("Attributes") or {})

    def list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 25,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": int(limit)}
        start_key = decode_token(next_token)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        if status:
            kwargs["IndexName"] = self.index_name
            kwargs["KeyConditionExpression"] = Key("status").eq(status)
            kwargs["ScanIndexForward"] = False
            response = self.table.query(**kwargs)
        else:
            response = self.table.scan(**kwargs)
        items = [to_native(item) for item in response.get("Items", [])]
        return items, encode_token(response.get("LastEvaluatedKey"))

    def ping(self) -> bool:
        try:
            return bool(self.table.table_status)
        except Exception as exc:
            LOGGER.warning("dynamodb ping failed: %s", exc)
            return False


class SqsJobQueue(JobQueue):
    """SQS backed job queue with dead-letter inspection support."""

    def __init__(
        self,
        queue_url: Optional[str] = None,
        dlq_url: Optional[str] = None,
        client: Any = None,
    ) -> None:
        self._client = client
        self._queue_url = queue_url or os.environ.get("JOBS_QUEUE_URL") or None
        self._dlq_url = dlq_url or os.environ.get("JOBS_DLQ_URL") or None
        self.queue_name = os.environ.get("JOBS_QUEUE_NAME", DEFAULT_QUEUE_NAME)
        self.dlq_name = os.environ.get("JOBS_DLQ_NAME", DEFAULT_DLQ_NAME)

    @property
    def client(self):
        """Lazily resolve the SQS client."""
        if self._client is None:
            self._client = sqs_client()
        return self._client

    @property
    def queue_url(self) -> str:
        """Resolve (and cache) the main queue URL."""
        if not self._queue_url:
            response = self.client.get_queue_url(QueueName=self.queue_name)
            self._queue_url = response["QueueUrl"]
        return self._queue_url

    @property
    def dlq_url(self) -> str:
        """Resolve (and cache) the dead-letter queue URL."""
        if not self._dlq_url:
            response = self.client.get_queue_url(QueueName=self.dlq_name)
            self._dlq_url = response["QueueUrl"]
        return self._dlq_url

    def send_job(self, message: Dict[str, Any]) -> str:
        response = self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(message, default=str),
        )
        return str(response.get("MessageId", ""))

    def peek_dead_letter(self, max_messages: int = 10) -> List[Dict[str, Any]]:
        bounded = max(1, min(int(max_messages), 10))
        response = self.client.receive_message(
            QueueUrl=self.dlq_url,
            MaxNumberOfMessages=bounded,
            VisibilityTimeout=int(os.environ.get("DLQ_PEEK_VISIBILITY", "1")),
            WaitTimeSeconds=0,
            AttributeNames=["All"],
        )
        messages: List[Dict[str, Any]] = []
        for raw in response.get("Messages", []):
            body = raw.get("Body", "")
            parsed: Optional[Dict[str, Any]] = None
            try:
                candidate = json.loads(body)
                if isinstance(candidate, dict):
                    parsed = candidate
            except (TypeError, ValueError):
                parsed = None
            attributes = raw.get("Attributes") or {}
            messages.append(
                {
                    "message_id": raw.get("MessageId"),
                    "job_id": (parsed or {}).get("job_id"),
                    "body": parsed,
                    "raw_body": None if parsed else body,
                    "approximate_receive_count": int(
                        attributes.get("ApproximateReceiveCount", 0) or 0
                    ),
                }
            )
        return messages

    def ping(self) -> bool:
        try:
            self.client.get_queue_attributes(
                QueueUrl=self.queue_url,
                AttributeNames=["QueueArn"],
            )
            return True
        except Exception as exc:
            LOGGER.warning("sqs ping failed: %s", exc)
            return False
