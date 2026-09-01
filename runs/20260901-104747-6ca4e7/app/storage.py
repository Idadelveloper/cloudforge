"""AWS-backed data access layer for the blog platform backend.

Every boto3 interaction lives behind a small class based interface so that the
HTTP layer (``app.py``) never talks to AWS directly and can be exercised with
in-memory fakes during tests.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

LOGGER = logging.getLogger(__name__)

DEFAULT_POSTS_TABLE = "blog-posts"
DEFAULT_COMMENTS_TABLE = "blog-published-comments"
DEFAULT_IMAGES_BUCKET = "blog-post-images"
DEFAULT_MODERATION_QUEUE = "blog-comment-moderation-queue"
DEFAULT_PRESIGN_TTL = 3600


class StorageError(RuntimeError):
    """Raised when an AWS backed operation fails."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


@contextmanager
def _aws_errors(operation: str):
    """Translate boto3 failures into :class:`StorageError`."""
    try:
        yield
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        raise StorageError("{0} failed ({1})".format(operation, code), code=code) from exc
    except BotoCoreError as exc:
        raise StorageError("{0} failed: {1}".format(operation, exc)) from exc


def utc_now_iso() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def aws_region() -> str:
    """Resolve the AWS region, defaulting to us-east-1."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def aws_endpoint_url() -> Optional[str]:
    """Resolve the AWS endpoint override (LocalStack friendly)."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource():
    """Build a DynamoDB service resource."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def s3_client():
    """Build an S3 client."""
    return boto3.client(
        "s3",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def sqs_client():
    """Build an SQS client."""
    return boto3.client(
        "sqs",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def to_plain(value: Any) -> Any:
    """Recursively convert DynamoDB types (Decimal, set) into JSON friendly ones."""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(to_plain(item) for item in value)
    return value


def encode_token(last_key: Dict[str, Any]) -> str:
    """Encode a DynamoDB LastEvaluatedKey into an opaque pagination token."""
    raw = json.dumps(to_plain(last_key), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: str) -> Dict[str, Any]:
    """Decode a pagination token produced by :func:`encode_token`."""
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid next_token") from exc
    if not isinstance(decoded, dict):
        raise ValueError("invalid next_token")
    return decoded


class DynamoPostRepository:
    """Post persistence backed by a DynamoDB table."""

    def __init__(self, table: Any = None, table_name: Optional[str] = None) -> None:
        self._table = table
        self.table_name = table_name or os.environ.get("POSTS_TABLE", DEFAULT_POSTS_TABLE)

    @property
    def table(self) -> Any:
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    def ping(self) -> bool:
        with _aws_errors("describe posts table"):
            return bool(self.table.table_status)

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        with _aws_errors("create post"):
            self.table.put_item(Item=item)
        return to_plain(item)

    def get(self, post_id: str) -> Optional[Dict[str, Any]]:
        with _aws_errors("get post"):
            response = self.table.get_item(Key={"post_id": post_id})
        item = response.get("Item")
        return to_plain(item) if item else None

    def list_posts(
        self,
        limit: int = 20,
        next_token: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"Limit": max(1, min(int(limit), 100))}
        if status:
            kwargs["FilterExpression"] = Attr("status").eq(status)
        if next_token:
            kwargs["ExclusiveStartKey"] = decode_token(next_token)
        with _aws_errors("list posts"):
            response = self.table.scan(**kwargs)
        items = [to_plain(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        return items, encode_token(last_key) if last_key else None

    def update(self, post_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not changes:
            raise ValueError("no attributes to update")
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments: List[str] = []
        for index, (attribute, value) in enumerate(changes.items()):
            names["#a{0}".format(index)] = attribute
            values[":v{0}".format(index)] = value
            assignments.append("#a{0} = :v{0}".format(index))
        try:
            response = self.table.update_item(
                Key={"post_id": post_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=Attr("post_id").exists(),
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise StorageError("update post failed", code="ClientError") from exc
        except BotoCoreError as exc:
            raise StorageError("update post failed: {0}".format(exc)) from exc
        return to_plain(response.get("Attributes", {}))

    def delete(self, post_id: str) -> bool:
        try:
            self.table.delete_item(
                Key={"post_id": post_id},
                ConditionExpression=Attr("post_id").exists(),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise StorageError("delete post failed", code="ClientError") from exc
        except BotoCoreError as exc:
            raise StorageError("delete post failed: {0}".format(exc)) from exc
        return True

    def add_image_key(self, post_id: str, image_key: str) -> bool:
        try:
            self.table.update_item(
                Key={"post_id": post_id},
                UpdateExpression=(
                    "SET image_keys = list_append(if_not_exists(image_keys, :empty), :new), "
                    "updated_at = :timestamp"
                ),
                ExpressionAttributeValues={
                    ":empty": [],
                    ":new": [image_key],
                    ":timestamp": utc_now_iso(),
                },
                ConditionExpression=Attr("post_id").exists(),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise StorageError("record image key failed", code="ClientError") from exc
        except BotoCoreError as exc:
            raise StorageError("record image key failed: {0}".format(exc)) from exc
        return True


class DynamoCommentRepository:
    """Published (moderated) comment persistence backed by DynamoDB."""

    def __init__(self, table: Any = None, table_name: Optional[str] = None) -> None:
        self._table = table
        self.table_name = table_name or os.environ.get("COMMENTS_TABLE", DEFAULT_COMMENTS_TABLE)

    @property
    def table(self) -> Any:
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    def ping(self) -> bool:
        with _aws_errors("describe comments table"):
            return bool(self.table.table_status)

    def put_comment(self, item: Dict[str, Any]) -> Dict[str, Any]:
        with _aws_errors("publish comment"):
            self.table.put_item(Item=item)
        return to_plain(item)

    def list_for_post(self, post_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with _aws_errors("list comments"):
            response = self.table.query(
                KeyConditionExpression=Key("post_id").eq(post_id),
                Limit=max(1, min(int(limit), 100)),
                ScanIndexForward=True,
            )
        return [to_plain(item) for item in response.get("Items", [])]


class S3ImageStore:
    """Image object storage backed by a private S3 bucket."""

    def __init__(
        self,
        client: Any = None,
        bucket: Optional[str] = None,
        presign_ttl: Optional[int] = None,
    ) -> None:
        self._client = client
        self.bucket = bucket or os.environ.get("IMAGES_BUCKET", DEFAULT_IMAGES_BUCKET)
        self.presign_ttl = int(presign_ttl or os.environ.get("PRESIGN_TTL", DEFAULT_PRESIGN_TTL))

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = s3_client()
        return self._client

    def ping(self) -> bool:
        with _aws_errors("head images bucket"):
            self.client.head_bucket(Bucket=self.bucket)
        return True

    def put_image(self, key: str, data: bytes, content_type: str) -> str:
        with _aws_errors("upload image"):
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        return key

    def list_keys(self, prefix: str) -> List[str]:
        keys: List[str] = []
        token: Optional[str] = None
        with _aws_errors("list images"):
            while True:
                kwargs: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                response = self.client.list_objects_v2(**kwargs)
                for entry in response.get("Contents", []):
                    keys.append(entry["Key"])
                if not response.get("IsTruncated"):
                    break
                token = response.get("NextContinuationToken")
                if not token:
                    break
        return keys

    def presigned_url(self, key: str, expires_in: Optional[int] = None) -> str:
        with _aws_errors("presign image url"):
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=int(expires_in or self.presign_ttl),
            )


class SqsModerationQueue:
    """Comment moderation queue backed by SQS."""

    def __init__(
        self,
        client: Any = None,
        queue_name: Optional[str] = None,
        queue_url: Optional[str] = None,
    ) -> None:
        self._client = client
        self.queue_name = queue_name or os.environ.get("MODERATION_QUEUE", DEFAULT_MODERATION_QUEUE)
        self._queue_url = queue_url or os.environ.get("MODERATION_QUEUE_URL") or None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = sqs_client()
        return self._client

    @property
    def queue_url(self) -> str:
        if not self._queue_url:
            with _aws_errors("resolve moderation queue url"):
                response = self.client.get_queue_url(QueueName=self.queue_name)
            self._queue_url = response["QueueUrl"]
        return self._queue_url

    def ping(self) -> bool:
        return bool(self.queue_url)

    def send_comment(self, payload: Dict[str, Any]) -> str:
        with _aws_errors("enqueue comment"):
            response = self.client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=json.dumps(payload),
            )
        return response.get("MessageId", "")

    def receive_comments(self, max_messages: int = 10, wait_seconds: int = 0) -> List[Dict[str, Any]]:
        with _aws_errors("receive comments"):
            response = self.client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=max(1, min(int(max_messages), 10)),
                WaitTimeSeconds=max(0, min(int(wait_seconds), 20)),
            )
        results: List[Dict[str, Any]] = []
        for message in response.get("Messages", []):
            raw_body = message.get("Body", "")
            try:
                payload = json.loads(raw_body)
            except (ValueError, TypeError):
                payload = None
            if not isinstance(payload, dict):
                payload = {"body": raw_body}
            entry = dict(payload)
            entry["receipt_handle"] = message.get("ReceiptHandle", "")
            entry["message_id"] = message.get("MessageId", "")
            results.append(entry)
        return results

    def delete_comment(self, receipt_handle: str) -> None:
        with _aws_errors("delete moderation message"):
            self.client.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=receipt_handle,
            )


@dataclass
class Storage:
    """Aggregate of the four collaborators used by the HTTP layer."""

    posts: Any
    comments: Any
    images: Any
    moderation: Any

    def health(self) -> Dict[str, Any]:
        """Report reachability of each dependency without raising."""
        checks: Dict[str, str] = {}
        healthy = True
        components = (
            ("posts", self.posts),
            ("comments", self.comments),
            ("images", self.images),
            ("moderation", self.moderation),
        )
        for name, component in components:
            ping = getattr(component, "ping", None)
            if ping is None:
                checks[name] = "skipped"
                continue
            try:
                ping()
                checks[name] = "ok"
            except Exception as exc:  # defensive: health must never fail hard
                LOGGER.warning("health check failed for %s: %s", name, exc)
                checks[name] = "unavailable"
                healthy = False
        return {
            "status": "ok" if healthy else "degraded",
            "service": "blog_platform_backend",
            "region": aws_region(),
            "endpoint_url": aws_endpoint_url(),
            "dependencies": checks,
        }


def build_storage() -> Storage:
    """Create the production (AWS backed) storage aggregate."""
    return Storage(
        posts=DynamoPostRepository(),
        comments=DynamoCommentRepository(),
        images=S3ImageStore(),
        moderation=SqsModerationQueue(),
    )
