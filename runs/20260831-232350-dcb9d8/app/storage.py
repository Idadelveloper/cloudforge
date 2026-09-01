"""Data access layer for the image gallery backend.

All AWS interaction (S3 for the image binaries, DynamoDB for album and image
metadata) is isolated in this module behind ``DynamoS3GalleryRepository`` so the
HTTP layer can be exercised with a simple in-memory fake in tests.

Environment variables:
    AWS_ENDPOINT_URL      -- override endpoint (LocalStack), unset in real AWS
    AWS_DEFAULT_REGION    -- defaults to us-east-1
    S3_BUCKET             -- defaults to image-gallery-media
    ALBUMS_TABLE          -- defaults to albums
    IMAGES_TABLE          -- defaults to images
    PRESIGN_EXPIRES_SECONDS -- defaults to 900
"""

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

LOGGER = logging.getLogger("image_gallery.storage")

DEFAULT_REGION = "us-east-1"
DEFAULT_BUCKET = "image-gallery-media"
DEFAULT_ALBUMS_TABLE = "albums"
DEFAULT_IMAGES_TABLE = "images"
DEFAULT_PRESIGN_EXPIRES = 900

STATUS_PENDING = "pending"
STATUS_AVAILABLE = "available"

NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket", "NotFound", "ResourceNotFoundException"}


class StorageError(Exception):
    """Base class for storage layer failures."""


class AlbumNotFound(StorageError):
    """Raised when an album does not exist."""


class ImageNotFound(StorageError):
    """Raised when an image metadata item does not exist."""


class ObjectNotUploaded(StorageError):
    """Raised when completing an upload whose S3 object is missing."""


def aws_region() -> str:
    """Region used by every AWS client."""
    return os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def aws_endpoint_url() -> Optional[str]:
    """Optional endpoint override (LocalStack)."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def s3_client() -> Any:
    """Build an S3 client honouring AWS_ENDPOINT_URL."""
    return boto3.client(
        "s3",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def dynamodb_resource() -> Any:
    """Build a DynamoDB resource honouring AWS_ENDPOINT_URL."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def utc_now_iso() -> str:
    """Current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def build_s3_key(album_id: str, image_id: str, filename: str) -> str:
    """Deterministic object key layout: albums/{album_id}/{image_id}/{filename}."""
    return "albums/{0}/{1}/{2}".format(album_id, image_id, filename)


def album_prefix(album_id: str) -> str:
    """S3 prefix holding every object of an album."""
    return "albums/{0}/".format(album_id)


def encode_token(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey as an opaque pagination token."""
    if not key:
        return None
    raw = json.dumps(key, default=str, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode a pagination token; returns None when malformed."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def error_code(exc: Exception) -> str:
    """Extract the AWS error code from a botocore ClientError-like exception."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def is_not_found(exc: Exception) -> bool:
    """True when the AWS exception means "the thing does not exist"."""
    return error_code(exc) in NOT_FOUND_CODES


def to_int(value: Any) -> int:
    """Coerce DynamoDB Decimals (and anything else) to a plain int."""
    if isinstance(value, Decimal):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _chunks(values: List[str], size: int) -> Iterator[List[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


class DynamoS3GalleryRepository:
    """Repository backed by two DynamoDB tables and one S3 bucket."""

    def __init__(
        self,
        bucket: Optional[str] = None,
        albums_table: Optional[str] = None,
        images_table: Optional[str] = None,
        presign_expires: Optional[int] = None,
        s3: Any = None,
        dynamodb: Any = None,
    ) -> None:
        self.bucket = bucket or os.environ.get("S3_BUCKET", DEFAULT_BUCKET)
        self.albums_table_name = albums_table or os.environ.get("ALBUMS_TABLE", DEFAULT_ALBUMS_TABLE)
        self.images_table_name = images_table or os.environ.get("IMAGES_TABLE", DEFAULT_IMAGES_TABLE)
        raw_expiry = presign_expires or os.environ.get("PRESIGN_EXPIRES_SECONDS", DEFAULT_PRESIGN_EXPIRES)
        self.presign_expires = to_int(raw_expiry) or DEFAULT_PRESIGN_EXPIRES
        self._s3 = s3
        self._dynamodb = dynamodb

    # ------------------------------------------------------------------
    # lazily-created AWS handles
    # ------------------------------------------------------------------
    @property
    def s3(self) -> Any:
        if self._s3 is None:
            self._s3 = s3_client()
        return self._s3

    @property
    def dynamodb(self) -> Any:
        if self._dynamodb is None:
            self._dynamodb = dynamodb_resource()
        return self._dynamodb

    @property
    def albums_table(self) -> Any:
        return self.dynamodb.Table(self.albums_table_name)

    @property
    def images_table(self) -> Any:
        return self.dynamodb.Table(self.images_table_name)

    # ------------------------------------------------------------------
    # serialisation helpers
    # ------------------------------------------------------------------
    def _album_out(self, item: Dict[str, Any]) -> Dict[str, Any]:
        created = str(item.get("created_at", "") or "")
        return {
            "album_id": str(item.get("album_id", "")),
            "title": str(item.get("title", "") or ""),
            "description": str(item.get("description", "") or ""),
            "image_count": to_int(item.get("image_count", 0)),
            "created_at": created,
            "updated_at": str(item.get("updated_at", created) or created),
        }

    def _image_out(self, item: Dict[str, Any], with_url: bool = True) -> Dict[str, Any]:
        uploaded_at = item.get("uploaded_at")
        out = {
            "album_id": str(item.get("album_id", "")),
            "image_id": str(item.get("image_id", "")),
            "filename": str(item.get("filename", "") or ""),
            "s3_key": str(item.get("s3_key", "") or ""),
            "content_type": str(item.get("content_type", "application/octet-stream") or ""),
            "size_bytes": to_int(item.get("size_bytes", 0)),
            "status": str(item.get("status", STATUS_PENDING) or STATUS_PENDING),
            "created_at": str(item.get("created_at", "") or ""),
            "uploaded_at": str(uploaded_at) if uploaded_at else None,
            "download_url": None,
        }
        if with_url and out["s3_key"] and out["status"] == STATUS_AVAILABLE:
            out["download_url"] = self.presigned_get_url(out["s3_key"])
        return out

    def presigned_get_url(self, key: str) -> Optional[str]:
        """Presigned GET URL for viewing/downloading an object."""
        try:
            return self.s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.presign_expires,
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("could not presign GET for %s: %s", key, exc)
            return None

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        checks: Dict[str, str] = {}
        healthy = True
        try:
            self.s3.head_bucket(Bucket=self.bucket)
            checks["s3"] = "ok"
        except Exception as exc:
            healthy = False
            checks["s3"] = "error"
            LOGGER.warning("S3 health check failed for %s: %s", self.bucket, exc)
        pairs = (
            ("albums_table", self.albums_table_name),
            ("images_table", self.images_table_name),
        )
        for label, table_name in pairs:
            try:
                self.dynamodb.meta.client.describe_table(TableName=table_name)
                checks[label] = "ok"
            except Exception as exc:
                healthy = False
                checks[label] = "error"
                LOGGER.warning("DynamoDB health check failed for %s: %s", table_name, exc)
        return {"ok": healthy, "checks": checks}

    # ------------------------------------------------------------------
    # albums
    # ------------------------------------------------------------------
    def create_album(self, title: str, description: str = "") -> Dict[str, Any]:
        now = utc_now_iso()
        item = {
            "album_id": str(uuid.uuid4()),
            "title": title,
            "description": description or "",
            "image_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        self.albums_table.put_item(Item=item)
        return self._album_out(item)

    def list_albums(self, limit: int = 50, next_token: Optional[str] = None) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"Limit": max(1, min(int(limit), 100))}
        start_key = decode_token(next_token)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = self.albums_table.scan(**kwargs)
        albums = [self._album_out(item) for item in response.get("Items", [])]
        return {
            "albums": albums,
            "next_token": encode_token(response.get("LastEvaluatedKey")),
        }

    def get_album(self, album_id: str) -> Optional[Dict[str, Any]]:
        response = self.albums_table.get_item(Key={"album_id": album_id})
        item = response.get("Item")
        return self._album_out(item) if item else None

    def update_album(self, album_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {k: v for k, v in updates.items() if k in ("title", "description") and v is not None}
        now = utc_now_iso()
        names = {"#updated_at": "updated_at"}
        values: Dict[str, Any] = {":updated_at": now}
        assignments = ["#updated_at = :updated_at"]
        for field, value in allowed.items():
            names["#" + field] = field
            values[":" + field] = value
            assignments.append("#{0} = :{0}".format(field))
        try:
            response = self.albums_table.update_item(
                Key={"album_id": album_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(album_id)",
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                raise AlbumNotFound(album_id) from exc
            raise
        attributes = response.get("Attributes") or {}
        attributes.setdefault("album_id", album_id)
        return self._album_out(attributes)

    def delete_album(self, album_id: str) -> None:
        if self.get_album(album_id) is None:
            raise AlbumNotFound(album_id)
        items = self._query_image_items(album_id)
        keys = [str(item.get("s3_key")) for item in items if item.get("s3_key")]
        keys.extend(self._list_object_keys(album_prefix(album_id)))
        self._delete_objects(sorted(set(keys)))
        if items:
            table = self.images_table
            with table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(
                        Key={"album_id": album_id, "image_id": item["image_id"]}
                    )
        self.albums_table.delete_item(Key={"album_id": album_id})

    def _increment_album_count(self, album_id: str, delta: int, now: str) -> None:
        if delta >= 0:
            expression = "SET image_count = if_not_exists(image_count, :zero) + :delta, updated_at = :now"
            condition = "attribute_exists(album_id)"
            values: Dict[str, Any] = {":zero": 0, ":delta": delta, ":now": now}
        else:
            expression = "SET image_count = if_not_exists(image_count, :zero) - :delta, updated_at = :now"
            condition = "attribute_exists(album_id) AND image_count > :zero"
            values = {":zero": 0, ":delta": abs(delta), ":now": now}
        try:
            self.albums_table.update_item(
                Key={"album_id": album_id},
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
        except Exception as exc:
            LOGGER.warning("image_count update skipped for album %s: %s", album_id, exc)

    # ------------------------------------------------------------------
    # images
    # ------------------------------------------------------------------
    def create_image(self, album_id: str, filename: str, content_type: str) -> Dict[str, Any]:
        if self.get_album(album_id) is None:
            raise AlbumNotFound(album_id)
        image_id = str(uuid.uuid4())
        key = build_s3_key(album_id, image_id, filename)
        now = utc_now_iso()
        item = {
            "album_id": album_id,
            "image_id": image_id,
            "filename": filename,
            "s3_key": key,
            "content_type": content_type or "application/octet-stream",
            "size_bytes": 0,
            "status": STATUS_PENDING,
            "created_at": now,
        }
        self.images_table.put_item(Item=item)
        self._increment_album_count(album_id, 1, now)
        upload_url = self.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": item["content_type"],
            },
            ExpiresIn=self.presign_expires,
        )
        return {
            "image": self._image_out(item, with_url=False),
            "upload_url": upload_url,
            "expires_in": self.presign_expires,
        }

    def complete_image(self, album_id: str, image_id: str) -> Dict[str, Any]:
        item = self._get_image_item(album_id, image_id)
        if item is None:
            raise ImageNotFound(image_id)
        key = str(item.get("s3_key", ""))
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if is_not_found(exc):
                raise ObjectNotUploaded(key) from exc
            raise
        now = utc_now_iso()
        size_bytes = to_int(head.get("ContentLength", 0))
        content_type = head.get("ContentType") or item.get("content_type") or "application/octet-stream"
        response = self.images_table.update_item(
            Key={"album_id": album_id, "image_id": image_id},
            UpdateExpression=(
                "SET #status = :status, #size = :size, #ctype = :ctype, #uploaded = :uploaded"
            ),
            ExpressionAttributeNames={
                "#status": "status",
                "#size": "size_bytes",
                "#ctype": "content_type",
                "#uploaded": "uploaded_at",
            },
            ExpressionAttributeValues={
                ":status": STATUS_AVAILABLE,
                ":size": size_bytes,
                ":ctype": content_type,
                ":uploaded": now,
            },
            ReturnValues="ALL_NEW",
        )
        attributes = response.get("Attributes") or {}
        merged = dict(item)
        merged.update(attributes)
        merged["status"] = STATUS_AVAILABLE
        merged["size_bytes"] = size_bytes
        merged["content_type"] = content_type
        merged["uploaded_at"] = now
        return self._image_out(merged)

    def list_images(self, album_id: str) -> List[Dict[str, Any]]:
        if self.get_album(album_id) is None:
            raise AlbumNotFound(album_id)
        items = self._query_image_items(album_id)
        return [self._image_out(item) for item in items]

    def get_image(self, album_id: str, image_id: str) -> Dict[str, Any]:
        item = self._get_image_item(album_id, image_id)
        if item is None:
            raise ImageNotFound(image_id)
        return self._image_out(item)

    def delete_image(self, album_id: str, image_id: str) -> None:
        item = self._get_image_item(album_id, image_id)
        if item is None:
            raise ImageNotFound(image_id)
        key = str(item.get("s3_key", ""))
        if key:
            self._delete_objects([key])
        self.images_table.delete_item(Key={"album_id": album_id, "image_id": image_id})
        self._increment_album_count(album_id, -1, utc_now_iso())

    # ------------------------------------------------------------------
    # low level helpers
    # ------------------------------------------------------------------
    def _get_image_item(self, album_id: str, image_id: str) -> Optional[Dict[str, Any]]:
        response = self.images_table.get_item(Key={"album_id": album_id, "image_id": image_id})
        return response.get("Item")

    def _query_image_items(self, album_id: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("album_id").eq(album_id)}
        while True:
            response = self.images_table.query(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items

    def _list_object_keys(self, prefix: str) -> List[str]:
        keys: List[str] = []
        kwargs: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
        while True:
            try:
                response = self.s3.list_objects_v2(**kwargs)
            except Exception as exc:
                LOGGER.warning("listing objects failed for %s: %s", prefix, exc)
                return keys
            for entry in response.get("Contents", []) or []:
                key = entry.get("Key")
                if key:
                    keys.append(str(key))
            token = response.get("NextContinuationToken")
            if not response.get("IsTruncated") or not token:
                break
            kwargs["ContinuationToken"] = token
        return keys

    def _delete_objects(self, keys: List[str]) -> None:
        if not keys:
            return
        for chunk in _chunks(list(keys), 1000):
            payload = {"Objects": [{"Key": key} for key in chunk], "Quiet": True}
            try:
                self.s3.delete_objects(Bucket=self.bucket, Delete=payload)
            except Exception as exc:
                LOGGER.warning("deleting %d objects failed: %s", len(chunk), exc)
