"""Data access layer for the blog platform backend.

All AWS interaction is funnelled through :class:`AwsBlogRepository`, which
implements the small :class:`BlogRepository` interface used by the HTTP layer.
Tests inject fake boto3 clients, so nothing here requires a network or a
running LocalStack instance.
"""
import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key

LOGGER = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_POSTS_TABLE = "blog-posts"
DEFAULT_COMMENTS_TABLE = "blog-comments"
DEFAULT_IMAGES_BUCKET = "blog-post-images"
DEFAULT_MODERATION_QUEUE = "blog-comment-moderation"
DEFAULT_PRESIGN_EXPIRY = "3600"
HEALTH_SENTINEL = "__health_check__"
SUMMARY_FIELDS = ("post_id", "title", "author", "tags", "status", "created_at", "updated_at")
UPDATABLE_FIELDS = ("title", "author", "body_markdown", "tags", "status")


class PostNotFound(Exception):
    """Raised when an operation targets a post that does not exist."""


class PendingCommentNotFound(Exception):
    """Raised when a moderation decision cannot be matched to a pending comment."""


def client_kwargs() -> Dict[str, Any]:
    """Shared boto3 keyword arguments, honouring AWS_ENDPOINT_URL for LocalStack."""
    return {
        "region_name": os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        "endpoint_url": os.environ.get("AWS_ENDPOINT_URL") or None,
    }


def dynamodb_resource():
    """Create a DynamoDB service resource."""
    return boto3.resource("dynamodb", **client_kwargs())


def s3_client():
    """Create an S3 client."""
    return boto3.client("s3", **client_kwargs())


def sqs_client():
    """Create an SQS client."""
    return boto3.client("sqs", **client_kwargs())


def utcnow_iso() -> str:
    """Current UTC time as a second-precision ISO-8601 string."""
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return stamp.replace("+00:00", "Z")


def to_plain(value: Any) -> Any:
    """Convert DynamoDB Decimals/sets into JSON friendly Python values."""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_plain(item) for item in value]
    return value


def encode_token(last_key: Dict[str, Any]) -> str:
    """Encode a DynamoDB LastEvaluatedKey into an opaque cursor."""
    raw = json.dumps(to_plain(last_key), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: str) -> Dict[str, Any]:
    """Decode an opaque cursor back into a DynamoDB ExclusiveStartKey."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid next_token") from exc
    if not isinstance(decoded, dict):
        raise ValueError("invalid next_token")
    return decoded


def safe_extension(filename: Optional[str]) -> str:
    """Return a sanitised file extension (including the dot) for an S3 key."""
    _, ext = os.path.splitext(filename or "")
    cleaned = "".join(char for char in ext if char.isalnum() or char == ".")
    return cleaned[:12].lower()


def parse_message_body(body: Optional[str]) -> Dict[str, Any]:
    """Best-effort JSON decode of an SQS message body."""
    if not body:
        return {}
    try:
        decoded = json.loads(body)
    except (ValueError, TypeError):
        return {"body": body}
    if isinstance(decoded, dict):
        return decoded
    return {"body": body}


class BlogRepository:
    """Interface describing the persistence operations the API needs."""

    def create_post(self, data: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def list_posts(self, limit: int = 20, next_token: Optional[str] = None,
                   status: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def get_post(self, post_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def update_post(self, post_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def delete_post(self, post_id: str) -> List[str]:
        raise NotImplementedError

    def add_image(self, post_id: str, filename: str, content_type: str, data: bytes) -> Dict[str, Any]:
        raise NotImplementedError

    def list_images(self, post_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def submit_comment(self, post_id: str, comment: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def list_comments(self, post_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def receive_pending_comments(self, max_messages: int = 10, visibility_timeout: int = 30,
                                 wait_seconds: int = 0) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def approve_comment(self, receipt_handle: str, comment_id: Optional[str] = None,
                        moderator: Optional[str] = None,
                        comment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def reject_comment(self, receipt_handle: str, comment_id: Optional[str] = None,
                       moderator: Optional[str] = None, reason: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:
        raise NotImplementedError


class AwsBlogRepository(BlogRepository):
    """DynamoDB + S3 + SQS backed implementation of :class:`BlogRepository`."""

    def __init__(self, posts_table: Any = None, comments_table: Any = None, s3: Any = None,
                 sqs: Any = None, bucket: Optional[str] = None, queue_url: Optional[str] = None,
                 presign_expiry: Optional[int] = None) -> None:
        self._posts_table = posts_table
        self._comments_table = comments_table
        self._s3 = s3
        self._sqs = sqs
        self._posts_table_name = os.environ.get("BLOG_POSTS_TABLE", DEFAULT_POSTS_TABLE)
        self._comments_table_name = os.environ.get("BLOG_COMMENTS_TABLE", DEFAULT_COMMENTS_TABLE)
        self._bucket = bucket or os.environ.get("BLOG_IMAGES_BUCKET", DEFAULT_IMAGES_BUCKET)
        self._queue_url = queue_url or os.environ.get("BLOG_MODERATION_QUEUE_URL") or None
        self._queue_name = os.environ.get("BLOG_MODERATION_QUEUE", DEFAULT_MODERATION_QUEUE)
        expiry = presign_expiry or os.environ.get("BLOG_PRESIGN_EXPIRY", DEFAULT_PRESIGN_EXPIRY)
        self._presign_expiry = int(expiry)

    # -- lazily created AWS handles -------------------------------------

    def _posts(self) -> Any:
        if self._posts_table is None:
            self._posts_table = dynamodb_resource().Table(self._posts_table_name)
        return self._posts_table

    def _comments(self) -> Any:
        if self._comments_table is None:
            self._comments_table = dynamodb_resource().Table(self._comments_table_name)
        return self._comments_table

    def _s3_client(self) -> Any:
        if self._s3 is None:
            self._s3 = s3_client()
        return self._s3

    def _sqs_client(self) -> Any:
        if self._sqs is None:
            self._sqs = sqs_client()
        return self._sqs

    def _queue(self) -> str:
        if not self._queue_url:
            response = self._sqs_client().get_queue_url(QueueName=self._queue_name)
            self._queue_url = response["QueueUrl"]
        return self._queue_url

    # -- posts ----------------------------------------------------------

    def create_post(self, data: Dict[str, Any]) -> Dict[str, Any]:
        now = utcnow_iso()
        item = {
            "post_id": data.get("post_id") or str(uuid.uuid4()),
            "title": data.get("title") or "untitled",
            "author": data.get("author") or "anonymous",
            "body_markdown": data.get("body_markdown") or "",
            "tags": list(data.get("tags") or []),
            "status": data.get("status") or "draft",
            "image_keys": [],
            "images": [],
            "created_at": now,
            "updated_at": now,
        }
        self._posts().put_item(Item=item)
        return to_plain(item)

    def list_posts(self, limit: int = 20, next_token: Optional[str] = None,
                   status: Optional[str] = None) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"Limit": max(1, min(int(limit), 100))}
        if next_token:
            kwargs["ExclusiveStartKey"] = decode_token(next_token)
        if status:
            kwargs["FilterExpression"] = Attr("status").eq(status)
        response = self._posts().scan(**kwargs)
        items = [to_plain(item) for item in response.get("Items", [])]
        if status:
            items = [item for item in items if item.get("status") == status]
        summaries = [{field: item.get(field) for field in SUMMARY_FIELDS} for item in items]
        summaries.sort(key=lambda entry: str(entry.get("created_at") or ""), reverse=True)
        token = response.get("LastEvaluatedKey")
        return {
            "items": summaries,
            "count": len(summaries),
            "next_token": encode_token(token) if token else None,
        }

    def get_post(self, post_id: str) -> Optional[Dict[str, Any]]:
        response = self._posts().get_item(Key={"post_id": post_id})
        item = response.get("Item")
        if not item:
            return None
        return to_plain(item)

    def update_post(self, post_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        if self.get_post(post_id) is None:
            raise PostNotFound(post_id)
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        clauses: List[str] = []
        index = 0
        for field in UPDATABLE_FIELDS:
            if changes.get(field) is None:
                continue
            name_key = "#f{}".format(index)
            value_key = ":v{}".format(index)
            names[name_key] = field
            values[value_key] = changes[field]
            clauses.append("{} = {}".format(name_key, value_key))
            index += 1
        if not clauses:
            raise ValueError("no updatable fields supplied")
        names["#updated_at"] = "updated_at"
        values[":updated_at"] = utcnow_iso()
        clauses.append("#updated_at = :updated_at")
        response = self._posts().update_item(
            Key={"post_id": post_id},
            UpdateExpression="SET " + ", ".join(clauses),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return to_plain(response.get("Attributes") or {})

    def delete_post(self, post_id: str) -> List[str]:
        post = self.get_post(post_id)
        if post is None:
            raise PostNotFound(post_id)
        keys = [str(key) for key in (post.get("image_keys") or [])]
        if keys:
            self._delete_objects(keys)
        self._posts().delete_item(Key={"post_id": post_id})
        LOGGER.info("deleted post %s and %d image object(s)", post_id, len(keys))
        return keys

    def _delete_objects(self, keys: List[str]) -> None:
        client = self._s3_client()
        for start in range(0, len(keys), 1000):
            batch = keys[start:start + 1000]
            client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": key} for key in batch]},
            )

    # -- images ---------------------------------------------------------

    def add_image(self, post_id: str, filename: str, content_type: str, data: bytes) -> Dict[str, Any]:
        if self.get_post(post_id) is None:
            raise PostNotFound(post_id)
        key = "posts/{}/{}{}".format(post_id, uuid.uuid4().hex, safe_extension(filename))
        self._s3_client().put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )
        now = utcnow_iso()
        metadata = {
            "post_id": post_id,
            "image_key": key,
            "content_type": content_type or "application/octet-stream",
            "size_bytes": len(data),
            "uploaded_at": now,
        }
        self._posts().update_item(
            Key={"post_id": post_id},
            UpdateExpression=(
                "SET image_keys = list_append(if_not_exists(image_keys, :empty), :key), "
                "images = list_append(if_not_exists(images, :empty), :meta), "
                "updated_at = :now"
            ),
            ExpressionAttributeValues={
                ":empty": [],
                ":key": [key],
                ":meta": [metadata],
                ":now": now,
            },
        )
        result = dict(metadata)
        result["download_url"] = self._presigned_url(key)
        return result

    def list_images(self, post_id: str) -> List[Dict[str, Any]]:
        post = self.get_post(post_id)
        if post is None:
            raise PostNotFound(post_id)
        metadata = {}
        for entry in post.get("images") or []:
            if isinstance(entry, dict) and entry.get("image_key"):
                metadata[entry["image_key"]] = entry
        images = []
        for key in post.get("image_keys") or []:
            meta = metadata.get(key, {})
            images.append({
                "post_id": post_id,
                "image_key": key,
                "content_type": meta.get("content_type") or "application/octet-stream",
                "size_bytes": int(meta.get("size_bytes") or 0),
                "uploaded_at": meta.get("uploaded_at"),
                "download_url": self._presigned_url(key),
            })
        return images

    def _presigned_url(self, key: str) -> Optional[str]:
        try:
            return self._s3_client().generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=self._presign_expiry,
            )
        except Exception as exc:  # noqa: BLE001 - presigning must never break a read
            LOGGER.warning("could not presign object %s: %s", key, exc)
            return None

    # -- comments -------------------------------------------------------

    def submit_comment(self, post_id: str, comment: Dict[str, Any]) -> Dict[str, Any]:
        if self.get_post(post_id) is None:
            raise PostNotFound(post_id)
        pending = {
            "comment_id": comment.get("comment_id") or str(uuid.uuid4()),
            "post_id": post_id,
            "author_name": comment.get("author_name") or "anonymous",
            "author_email": comment.get("author_email"),
            "body": comment.get("body") or "",
            "submitted_at": utcnow_iso(),
        }
        response = self._sqs_client().send_message(
            QueueUrl=self._queue(),
            MessageBody=json.dumps(pending),
        )
        result = dict(pending)
        result["message_id"] = response.get("MessageId")
        LOGGER.info("queued comment %s for moderation", pending["comment_id"])
        return result

    def list_comments(self, post_id: str) -> List[Dict[str, Any]]:
        response = self._comments().query(
            KeyConditionExpression=Key("post_id").eq(post_id),
        )
        items = [to_plain(item) for item in response.get("Items", [])]
        items = [item for item in items if item.get("post_id") == post_id]
        items.sort(key=lambda entry: str(entry.get("submitted_at") or ""))
        return items

    def receive_pending_comments(self, max_messages: int = 10, visibility_timeout: int = 30,
                                 wait_seconds: int = 0) -> List[Dict[str, Any]]:
        response = self._sqs_client().receive_message(
            QueueUrl=self._queue(),
            MaxNumberOfMessages=max(1, min(int(max_messages), 10)),
            VisibilityTimeout=max(0, int(visibility_timeout)),
            WaitTimeSeconds=max(0, min(int(wait_seconds), 20)),
        )
        pending = []
        for message in response.get("Messages", []):
            comment = parse_message_body(message.get("Body"))
            comment["receipt_handle"] = message.get("ReceiptHandle")
            comment["message_id"] = message.get("MessageId")
            pending.append(comment)
        return pending

    def _find_pending_in_queue(self, comment_id: str) -> Optional[Dict[str, Any]]:
        response = self._sqs_client().receive_message(
            QueueUrl=self._queue(),
            MaxNumberOfMessages=10,
            VisibilityTimeout=0,
            WaitTimeSeconds=0,
        )
        for message in response.get("Messages", []):
            comment = parse_message_body(message.get("Body"))
            if comment.get("comment_id") == comment_id:
                return comment
        return None

    def _resolve_pending(self, comment_id: Optional[str],
                         comment: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if comment and comment.get("post_id") and comment.get("body"):
            return dict(comment)
        if comment_id:
            found = self._find_pending_in_queue(comment_id)
            if found is not None:
                return found
        raise PendingCommentNotFound(comment_id or "unknown")

    def approve_comment(self, receipt_handle: str, comment_id: Optional[str] = None,
                        moderator: Optional[str] = None,
                        comment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resolved = self._resolve_pending(comment_id, comment)
        now = utcnow_iso()
        item: Dict[str, Any] = {
            "post_id": resolved["post_id"],
            "comment_id": resolved.get("comment_id") or comment_id or str(uuid.uuid4()),
            "author_name": resolved.get("author_name") or "anonymous",
            "body": resolved.get("body") or "",
            "submitted_at": resolved.get("submitted_at") or now,
            "approved_at": now,
        }
        if resolved.get("author_email"):
            item["author_email"] = resolved["author_email"]
        if moderator:
            item["moderator"] = moderator
        self._comments().put_item(Item=item)
        self._sqs_client().delete_message(QueueUrl=self._queue(), ReceiptHandle=receipt_handle)
        LOGGER.info("approved comment %s on post %s", item["comment_id"], item["post_id"])
        return to_plain(item)

    def reject_comment(self, receipt_handle: str, comment_id: Optional[str] = None,
                       moderator: Optional[str] = None, reason: Optional[str] = None) -> Dict[str, Any]:
        self._sqs_client().delete_message(QueueUrl=self._queue(), ReceiptHandle=receipt_handle)
        LOGGER.info("rejected comment %s (moderator=%s reason=%s)", comment_id, moderator, reason)
        return {
            "status": "rejected",
            "comment_id": comment_id,
            "moderator": moderator,
            "reason": reason,
            "rejected_at": utcnow_iso(),
        }

    # -- health ---------------------------------------------------------

    def _check_dynamodb(self) -> None:
        self._posts().get_item(Key={"post_id": HEALTH_SENTINEL})
        self._comments().get_item(Key={"post_id": HEALTH_SENTINEL, "comment_id": HEALTH_SENTINEL})

    def _check_s3(self) -> None:
        self._s3_client().head_bucket(Bucket=self._bucket)

    def _check_sqs(self) -> None:
        self._sqs_client().get_queue_attributes(
            QueueUrl=self._queue(),
            AttributeNames=["ApproximateNumberOfMessages"],
        )

    def health(self) -> Dict[str, Any]:
        checks = {}
        probes = (
            ("dynamodb", self._check_dynamodb),
            ("s3", self._check_s3),
            ("sqs", self._check_sqs),
        )
        for name, probe in probes:
            try:
                probe()
                checks[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - health must report, not raise
                LOGGER.warning("health probe for %s failed: %s", name, exc)
                checks[name] = "unavailable"
        overall = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
        return {
            "status": overall,
            "services": checks,
            "resources": {
                "posts_table": self._posts_table_name,
                "comments_table": self._comments_table_name,
                "images_bucket": self._bucket,
                "moderation_queue": self._queue_name,
            },
            "checked_at": utcnow_iso(),
        }
