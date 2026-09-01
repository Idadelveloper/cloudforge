"""AWS data-access layer for the image gallery backend.

Everything that talks to S3, DynamoDB or Secrets Manager lives here behind a
small repository interface so the HTTP layer stays testable offline.
"""

import base64
import binascii
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

LOGGER = logging.getLogger("image_gallery.storage")

DEFAULT_REGION = "us-east-1"
STATUS_PENDING = "pending"
STATUS_AVAILABLE = "available"
_BATCH_DELETE_SIZE = 1000


class GalleryError(Exception):
    """Base class for gallery errors."""


class NotFoundError(GalleryError):
    """Requested album or image does not exist."""


class ConflictError(GalleryError):
    """Requested state transition is not possible."""


class BadRequestError(GalleryError):
    """Client supplied invalid input."""


class StorageError(GalleryError):
    """Underlying AWS call failed."""


def _region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or DEFAULT_REGION


def _endpoint() -> Optional[str]:
    return os.environ.get("AWS_ENDPOINT_URL") or None


def s3_client():
    """Create an S3 client honouring AWS_ENDPOINT_URL (LocalStack)."""
    return boto3.client("s3", region_name=_region(), endpoint_url=_endpoint())


def dynamodb_resource():
    """Create a DynamoDB resource honouring AWS_ENDPOINT_URL (LocalStack)."""
    return boto3.resource("dynamodb", region_name=_region(), endpoint_url=_endpoint())


def secretsmanager_client():
    """Create a Secrets Manager client honouring AWS_ENDPOINT_URL."""
    return boto3.client("secretsmanager", region_name=_region(), endpoint_url=_endpoint())


def utc_now_iso() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def encode_token(last_key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey into an opaque cursor."""
    if not last_key:
        return None
    raw = json.dumps(_plain(last_key), sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode an opaque cursor back into a DynamoDB ExclusiveStartKey."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise BadRequestError("invalid next_token") from exc
    if not isinstance(data, dict):
        raise BadRequestError("invalid next_token")
    return data


def _plain(value: Any) -> Any:
    """Convert DynamoDB Decimals into plain JSON-friendly types."""
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def safe_filename(filename: str) -> str:
    """Strip path components and unsafe characters from an uploaded filename."""
    name = os.path.basename((filename or "").strip().replace("\\", "/"))
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "._- ")
    cleaned = cleaned.strip().replace(" ", "_").lstrip(".")
    return cleaned[:200] or "upload.bin"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        LOGGER.warning("invalid integer for %s, using default %s", name, default)
        return default


@dataclass
class Settings:
    """Runtime configuration for the service."""

    bucket: str = "image-gallery-media"
    albums_table: str = "image-gallery-albums"
    images_table: str = "image-gallery-images"
    presign_ttl: int = 900


def _apply_secret_overrides(settings: Settings, secret_name: str) -> None:
    """Merge configuration coming from Secrets Manager (best effort)."""
    try:
        response = secretsmanager_client().get_secret_value(SecretId=secret_name)
        payload = json.loads(response.get("SecretString") or "{}")
    except (ClientError, BotoCoreError, ValueError, TypeError) as exc:
        LOGGER.warning("could not load config secret %s: %s", secret_name, exc)
        return
    if not isinstance(payload, dict):
        LOGGER.warning("config secret %s is not a JSON object", secret_name)
        return
    settings.bucket = str(payload.get("bucket", settings.bucket))
    settings.albums_table = str(payload.get("albums_table", settings.albums_table))
    settings.images_table = str(payload.get("images_table", settings.images_table))
    try:
        settings.presign_ttl = int(payload.get("presign_ttl", settings.presign_ttl))
    except (TypeError, ValueError):
        LOGGER.warning("invalid presign_ttl in secret %s", secret_name)


def load_settings() -> Settings:
    """Build settings from environment variables (optionally Secrets Manager)."""
    settings = Settings(
        bucket=os.environ.get("GALLERY_BUCKET", "image-gallery-media"),
        albums_table=os.environ.get("ALBUMS_TABLE", "image-gallery-albums"),
        images_table=os.environ.get("IMAGES_TABLE", "image-gallery-images"),
        presign_ttl=_int_env("PRESIGN_TTL_SECONDS", 900),
    )
    secret_name = os.environ.get("APP_CONFIG_SECRET", "").strip()
    if secret_name:
        _apply_secret_overrides(settings, secret_name)
    return settings


class GalleryRepository:
    """Repository encapsulating all S3 / DynamoDB access."""

    def __init__(self, settings: Settings, s3: Any = None, dynamodb: Any = None) -> None:
        self._settings = settings
        self._s3 = s3
        self._dynamodb = dynamodb
        self._albums_table = None
        self._images_table = None

    # ---------------------------------------------------------------- clients
    @property
    def settings(self) -> Settings:
        """Return the active settings."""
        return self._settings

    @property
    def s3(self) -> Any:
        """Lazily created S3 client."""
        if self._s3 is None:
            self._s3 = s3_client()
        return self._s3

    @property
    def dynamodb(self) -> Any:
        """Lazily created DynamoDB resource."""
        if self._dynamodb is None:
            self._dynamodb = dynamodb_resource()
        return self._dynamodb

    @property
    def albums_table(self) -> Any:
        """DynamoDB table holding album metadata."""
        if self._albums_table is None:
            self._albums_table = self.dynamodb.Table(self._settings.albums_table)
        return self._albums_table

    @property
    def images_table(self) -> Any:
        """DynamoDB table holding image metadata."""
        if self._images_table is None:
            self._images_table = self.dynamodb.Table(self._settings.images_table)
        return self._images_table

    # ----------------------------------------------------------------- health
    def health(self) -> Dict[str, Any]:
        """Check that the bucket and both tables are reachable."""
        checks = {
            "s3": self._check(lambda: self.s3.head_bucket(Bucket=self._settings.bucket)),
            "albums_table": self._check(self.albums_table.load),
            "images_table": self._check(self.images_table.load),
        }
        healthy = all(value == "ok" for value in checks.values())
        return {
            "status": "ok" if healthy else "degraded",
            "checks": checks,
            "bucket": self._settings.bucket,
            "albums_table": self._settings.albums_table,
            "images_table": self._settings.images_table,
        }

    @staticmethod
    def _check(call: Any) -> str:
        try:
            call()
            return "ok"
        except Exception as exc:  # defensive: health must never raise
            LOGGER.warning("health check failed: %s", exc)
            return "error"

    # ----------------------------------------------------------------- albums
    def create_album(self, title: str, description: Optional[str] = None) -> Dict[str, Any]:
        """Persist a new album item and return it."""
        now = utc_now_iso()
        item = {
            "album_id": str(uuid.uuid4()),
            "title": title,
            "description": description,
            "image_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        try:
            self.albums_table.put_item(Item=item)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not create album: {exc}") from exc
        LOGGER.info("created album %s", item["album_id"])
        return self._album_view(item)

    def list_albums(
        self,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Scan the albums table with cursor pagination."""
        kwargs: Dict[str, Any] = {"Limit": max(1, min(int(limit), 200))}
        start_key = decode_token(next_token)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        try:
            response = self.albums_table.scan(**kwargs)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not list albums: {exc}") from exc
        items = [self._album_view(item) for item in response.get("Items", [])]
        items.sort(key=lambda album: album.get("created_at") or "")
        return items, encode_token(response.get("LastEvaluatedKey"))

    def get_album(self, album_id: str) -> Dict[str, Any]:
        """Fetch one album or raise NotFoundError."""
        return self._album_view(self._get_album_item(album_id))

    def _get_album_item(self, album_id: str) -> Dict[str, Any]:
        try:
            response = self.albums_table.get_item(Key={"album_id": album_id})
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not read album: {exc}") from exc
        item = response.get("Item")
        if not item:
            raise NotFoundError(f"album '{album_id}' not found")
        return _plain(item)

    def update_album(
        self,
        album_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update album title/description."""
        names: Dict[str, str] = {"#updated_at": "updated_at"}
        values: Dict[str, Any] = {":updated_at": utc_now_iso()}
        expressions = ["#updated_at = :updated_at"]
        if title is not None:
            names["#title"] = "title"
            values[":title"] = title
            expressions.append("#title = :title")
        if description is not None:
            names["#description"] = "description"
            values[":description"] = description
            expressions.append("#description = :description")
        try:
            response = self.albums_table.update_item(
                Key={"album_id": album_id},
                UpdateExpression="SET " + ", ".join(expressions),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(album_id)",
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if self._error_code(exc) == "ConditionalCheckFailedException":
                raise NotFoundError(f"album '{album_id}' not found") from exc
            raise StorageError(f"could not update album: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"could not update album: {exc}") from exc
        attributes = response.get("Attributes")
        if not attributes:
            return self.get_album(album_id)
        return self._album_view(_plain(attributes))

    def delete_album(self, album_id: str) -> Dict[str, Any]:
        """Delete an album, its image items and all of its S3 objects."""
        self._get_album_item(album_id)
        images = self._all_image_items(album_id)
        keys = [item["s3_key"] for item in images if item.get("s3_key")]
        deleted_objects = self._delete_objects(keys)
        self._delete_image_items(album_id, images)
        try:
            self.albums_table.delete_item(Key={"album_id": album_id})
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not delete album: {exc}") from exc
        LOGGER.info("deleted album %s with %d images", album_id, len(images))
        return {
            "album_id": album_id,
            "deleted": True,
            "deleted_images": len(images),
            "deleted_objects": deleted_objects,
        }

    # ----------------------------------------------------------------- images
    def create_pending_image(
        self,
        album_id: str,
        filename: str,
        content_type: str = "application/octet-stream",
        size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Register a pending image item and return presigned upload details."""
        self._get_album_item(album_id)
        image_id = str(uuid.uuid4())
        name = safe_filename(filename)
        s3_key = "albums/{}/{}/{}".format(album_id, image_id, name)
        item = {
            "album_id": album_id,
            "image_id": image_id,
            "filename": name,
            "content_type": content_type or "application/octet-stream",
            "s3_key": s3_key,
            "size_bytes": int(size_bytes) if size_bytes is not None else None,
            "etag": None,
            "status": STATUS_PENDING,
            "created_at": utc_now_iso(),
            "uploaded_at": None,
        }
        try:
            self.images_table.put_item(Item=item)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not register image: {exc}") from exc
        upload_url = self._presigned_url("put_object", s3_key, content_type=item["content_type"])
        LOGGER.info("registered pending image %s in album %s", image_id, album_id)
        return {
            "image_id": image_id,
            "upload_url": upload_url,
            "method": "PUT",
            "s3_key": s3_key,
            "expires_in": self._settings.presign_ttl,
            "content_type": item["content_type"],
        }

    def complete_image(self, album_id: str, image_id: str) -> Dict[str, Any]:
        """Verify the object exists in S3 and mark the image available."""
        item = self._get_image_item(album_id, image_id)
        head = self._head_object(item["s3_key"])
        now = utc_now_iso()
        try:
            size = int(head.get("ContentLength") or 0)
        except (TypeError, ValueError):
            size = 0
        etag = str(head.get("ETag") or "").strip('"')
        content_type = head.get("ContentType") or item.get("content_type") or "application/octet-stream"
        try:
            self.images_table.update_item(
                Key={"album_id": album_id, "image_id": image_id},
                UpdateExpression=(
                    "SET #status = :status, size_bytes = :size, etag = :etag, "
                    "content_type = :content_type, uploaded_at = :now"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": STATUS_AVAILABLE,
                    ":size": size,
                    ":etag": etag,
                    ":content_type": content_type,
                    ":now": now,
                },
                ConditionExpression="attribute_exists(image_id)",
            )
        except ClientError as exc:
            if self._error_code(exc) == "ConditionalCheckFailedException":
                raise NotFoundError(f"image '{image_id}' not found") from exc
            raise StorageError(f"could not update image: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"could not update image: {exc}") from exc
        was_pending = item.get("status") != STATUS_AVAILABLE
        item.update(
            {
                "status": STATUS_AVAILABLE,
                "size_bytes": size,
                "etag": etag,
                "content_type": content_type,
                "uploaded_at": now,
            }
        )
        if was_pending:
            self._increment_album_count(album_id, 1)
        LOGGER.info("image %s in album %s marked available", image_id, album_id)
        return self._image_view(item)

    def list_images(
        self,
        album_id: str,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Query the images of an album, adding presigned download URLs."""
        self._get_album_item(album_id)
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("album_id").eq(album_id),
            "Limit": max(1, min(int(limit), 200)),
        }
        start_key = decode_token(next_token)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        try:
            response = self.images_table.query(**kwargs)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not list images: {exc}") from exc
        items = [self._image_view(_plain(item)) for item in response.get("Items", [])]
        return items, encode_token(response.get("LastEvaluatedKey"))

    def get_image(self, album_id: str, image_id: str) -> Dict[str, Any]:
        """Fetch a single image with a fresh presigned download URL."""
        return self._image_view(self._get_image_item(album_id, image_id))

    def delete_image(self, album_id: str, image_id: str) -> Dict[str, Any]:
        """Delete an image object and its metadata item."""
        item = self._get_image_item(album_id, image_id)
        self._delete_objects([item["s3_key"]] if item.get("s3_key") else [])
        try:
            self.images_table.delete_item(Key={"album_id": album_id, "image_id": image_id})
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not delete image: {exc}") from exc
        if item.get("status") == STATUS_AVAILABLE:
            self._increment_album_count(album_id, -1)
        LOGGER.info("deleted image %s from album %s", image_id, album_id)
        return {"album_id": album_id, "image_id": image_id, "deleted": True}

    # ---------------------------------------------------------------- helpers
    def _get_image_item(self, album_id: str, image_id: str) -> Dict[str, Any]:
        try:
            response = self.images_table.get_item(Key={"album_id": album_id, "image_id": image_id})
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not read image: {exc}") from exc
        item = response.get("Item")
        if not item:
            raise NotFoundError(f"image '{image_id}' not found in album '{album_id}'")
        return _plain(item)

    def _all_image_items(self, album_id: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        start_key: Optional[Dict[str, Any]] = None
        while True:
            kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("album_id").eq(album_id)}
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            try:
                response = self.images_table.query(**kwargs)
            except (ClientError, BotoCoreError) as exc:
                raise StorageError(f"could not list images: {exc}") from exc
            items.extend(_plain(item) for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return items

    def _delete_image_items(self, album_id: str, items: List[Dict[str, Any]]) -> None:
        if not items:
            return
        try:
            with self.images_table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"album_id": album_id, "image_id": item["image_id"]})
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not delete image metadata: {exc}") from exc

    def _delete_objects(self, keys: List[str]) -> int:
        deleted = 0
        for index in range(0, len(keys), _BATCH_DELETE_SIZE):
            chunk = keys[index:index + _BATCH_DELETE_SIZE]
            try:
                self.s3.delete_objects(
                    Bucket=self._settings.bucket,
                    Delete={"Objects": [{"Key": key} for key in chunk], "Quiet": True},
                )
                deleted += len(chunk)
            except (ClientError, BotoCoreError) as exc:
                LOGGER.warning("failed to delete %d S3 objects: %s", len(chunk), exc)
        return deleted

    def _head_object(self, s3_key: str) -> Dict[str, Any]:
        try:
            return self.s3.head_object(Bucket=self._settings.bucket, Key=s3_key)
        except ClientError as exc:
            code = self._error_code(exc)
            if code in ("404", "NoSuchKey", "NotFound", "403"):
                raise ConflictError(f"object '{s3_key}' has not been uploaded yet") from exc
            raise StorageError(f"could not inspect object: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"could not inspect object: {exc}") from exc

    def _increment_album_count(self, album_id: str, delta: int) -> None:
        now = utc_now_iso()
        if delta >= 0:
            expression = "SET image_count = if_not_exists(image_count, :zero) + :delta, updated_at = :now"
            condition = "attribute_exists(album_id)"
            values = {":zero": 0, ":delta": delta, ":now": now}
        else:
            expression = "SET image_count = image_count - :delta, updated_at = :now"
            condition = "attribute_exists(album_id) AND image_count >= :delta"
            values = {":delta": abs(delta), ":now": now}
        try:
            self.albums_table.update_item(
                Key={"album_id": album_id},
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            LOGGER.warning("could not adjust image_count for album %s: %s", album_id, exc)
        except BotoCoreError as exc:
            LOGGER.warning("could not adjust image_count for album %s: %s", album_id, exc)

    def _presigned_url(self, operation: str, s3_key: str, content_type: Optional[str] = None) -> str:
        params: Dict[str, Any] = {"Bucket": self._settings.bucket, "Key": s3_key}
        if operation == "put_object" and content_type:
            params["ContentType"] = content_type
        try:
            return self.s3.generate_presigned_url(
                operation,
                Params=params,
                ExpiresIn=self._settings.presign_ttl,
            )
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not create presigned url: {exc}") from exc

    @staticmethod
    def _error_code(exc: ClientError) -> str:
        response = getattr(exc, "response", None) or {}
        return str(response.get("Error", {}).get("Code", ""))

    @staticmethod
    def _album_view(item: Dict[str, Any]) -> Dict[str, Any]:
        data = _plain(item)
        return {
            "album_id": data.get("album_id", ""),
            "title": data.get("title", ""),
            "description": data.get("description"),
            "image_count": int(data.get("image_count") or 0),
            "created_at": data.get("created_at", ""),
            "updated_at": data.get("updated_at", data.get("created_at", "")),
        }

    def _image_view(self, item: Dict[str, Any]) -> Dict[str, Any]:
        data = _plain(item)
        s3_key = data.get("s3_key", "")
        download_url = self._presigned_url("get_object", s3_key) if s3_key else None
        size = data.get("size_bytes")
        return {
            "album_id": data.get("album_id", ""),
            "image_id": data.get("image_id", ""),
            "filename": data.get("filename", ""),
            "content_type": data.get("content_type", "application/octet-stream"),
            "size_bytes": int(size) if size is not None else None,
            "etag": data.get("etag"),
            "status": data.get("status", STATUS_PENDING),
            "created_at": data.get("created_at", ""),
            "uploaded_at": data.get("uploaded_at"),
            "download_url": download_url,
            "s3_key": s3_key,
        }
