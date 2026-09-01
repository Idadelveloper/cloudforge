"""Data access layer: S3 objects plus DynamoDB metadata for the file sharing backend."""

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key

LOGGER = logging.getLogger("file_sharing_backend.storage")

DEFAULT_REGION = "us-east-1"
DEFAULT_BUCKET = "file-sharing-objects"
DEFAULT_TABLE = "file-metadata"
DEFAULT_OWNER_INDEX = "owner-index"
DEFAULT_EXPIRES_IN = 900
MAX_PAGE_SIZE = 100

NOT_FOUND_CODES = {
    "404",
    "NotFound",
    "NoSuchKey",
    "NoSuchBucket",
    "ResourceNotFoundException",
}


class StorageError(Exception):
    """Raised when an AWS backed operation fails."""


class NotFoundError(StorageError):
    """Raised when a requested file or object does not exist."""


class InvalidTokenError(StorageError):
    """Raised when a pagination token cannot be decoded."""


def aws_region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)


def aws_endpoint_url() -> Optional[str]:
    return os.environ.get("AWS_ENDPOINT_URL") or None


def bucket_name() -> str:
    return os.environ.get("S3_BUCKET", DEFAULT_BUCKET)


def table_name() -> str:
    return os.environ.get("DYNAMODB_TABLE", DEFAULT_TABLE)


def owner_index_name() -> str:
    return os.environ.get("DYNAMODB_OWNER_INDEX", DEFAULT_OWNER_INDEX)


def presign_expires_in() -> int:
    raw = os.environ.get("PRESIGN_EXPIRES_IN", str(DEFAULT_EXPIRES_IN))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_EXPIRES_IN
    return value if value > 0 else DEFAULT_EXPIRES_IN


def s3_client():
    """Build an S3 client honouring AWS_ENDPOINT_URL (LocalStack friendly)."""
    return boto3.client(
        "s3",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def dynamodb_resource():
    """Build a DynamoDB resource honouring AWS_ENDPOINT_URL (LocalStack friendly)."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def encode_token(key: Dict[str, Any]) -> str:
    raw = json.dumps(key, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: str) -> Dict[str, Any]:
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any malformed token is a client error
        raise InvalidTokenError("invalid next_token") from exc
    if not isinstance(data, dict):
        raise InvalidTokenError("invalid next_token")
    return data


def safe_filename(filename: str) -> str:
    cleaned = filename.replace("\\", "/").split("/")[-1].strip()
    return cleaned or "file"


def _clean(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert DynamoDB Decimals into plain Python numbers."""
    result: Dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, Decimal):
            result[key] = int(value) if value == value.to_integral_value() else float(value)
        else:
            result[key] = value
    return result


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def _wrap(exc: Exception, message: str) -> StorageError:
    if isinstance(exc, StorageError):
        return exc
    if _error_code(exc) in NOT_FOUND_CODES:
        return NotFoundError(message)
    return StorageError("{0}: {1}".format(message, exc))


class FileStore(object):
    """Interface implemented by the AWS adapter and by test doubles."""

    def create_upload_url(
        self,
        owner: str,
        filename: str,
        content_type: str = "application/octet-stream",
        size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def complete_upload(self, file_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def get_file(self, file_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def download_url(self, s3_key: str) -> str:
        raise NotImplementedError

    def list_files(
        self,
        owner: str,
        limit: int = 25,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        raise NotImplementedError

    def delete_file(self, file_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def usage(self, owner: Optional[str] = None) -> Tuple[List[Dict[str, Any]], int]:
        raise NotImplementedError


class DynamoS3FileStore(FileStore):
    """Concrete store backed by S3 (objects) and DynamoDB (metadata)."""

    def __init__(
        self,
        s3=None,
        table=None,
        bucket: Optional[str] = None,
        owner_index: Optional[str] = None,
        expires_in: Optional[int] = None,
    ) -> None:
        self._s3 = s3
        self._table = table
        self.bucket = bucket or bucket_name()
        self.owner_index = owner_index or owner_index_name()
        self.expires_in = int(expires_in or presign_expires_in())

    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = s3_client()
        return self._s3

    @property
    def table(self):
        if self._table is None:
            self._table = dynamodb_resource().Table(table_name())
        return self._table

    def create_upload_url(
        self,
        owner: str,
        filename: str,
        content_type: str = "application/octet-stream",
        size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        file_id = str(uuid.uuid4())
        clean_name = safe_filename(filename)
        s3_key = "{0}/{1}/{2}".format(owner, file_id, clean_name)
        now = utcnow_iso()
        item = {
            "file_id": file_id,
            "owner": owner,
            "filename": clean_name,
            "content_type": content_type,
            "size_bytes": int(size_bytes or 0),
            "s3_key": s3_key,
            "status": "pending",
            "created_at": now,
            "uploaded_at": now,
        }
        try:
            self.table.put_item(Item=item)
        except Exception as exc:
            raise _wrap(exc, "failed to store metadata")
        try:
            url = self.s3.generate_presigned_url(
                ClientMethod="put_object",
                Params={"Bucket": self.bucket, "Key": s3_key, "ContentType": content_type},
                ExpiresIn=self.expires_in,
            )
        except Exception as exc:
            raise _wrap(exc, "failed to generate upload url")
        return {
            "file_id": file_id,
            "upload_url": url,
            "s3_key": s3_key,
            "expires_in": self.expires_in,
        }

    def get_file(self, file_id: str) -> Dict[str, Any]:
        try:
            response = self.table.get_item(Key={"file_id": file_id})
        except Exception as exc:
            raise _wrap(exc, "failed to read metadata")
        item = (response or {}).get("Item")
        if not item:
            raise NotFoundError("file {0} not found".format(file_id))
        return _clean(item)

    def _head_size(self, s3_key: str) -> int:
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=s3_key)
        except Exception as exc:
            raise _wrap(exc, "object not uploaded for key {0}".format(s3_key))
        return int((head or {}).get("ContentLength", 0) or 0)

    def complete_upload(self, file_id: str) -> Dict[str, Any]:
        item = self.get_file(file_id)
        size = self._head_size(str(item.get("s3_key", "")))
        now = utcnow_iso()
        try:
            response = self.table.update_item(
                Key={"file_id": file_id},
                UpdateExpression="SET #st = :st, size_bytes = :sz, uploaded_at = :ts",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":st": "available", ":sz": size, ":ts": now},
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            raise _wrap(exc, "failed to update metadata")
        attributes = (response or {}).get("Attributes")
        if attributes:
            return _clean(attributes)
        item.update({"status": "available", "size_bytes": size, "uploaded_at": now})
        return item

    def download_url(self, s3_key: str) -> str:
        try:
            return self.s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": self.bucket, "Key": s3_key},
                ExpiresIn=self.expires_in,
            )
        except Exception as exc:
            raise _wrap(exc, "failed to generate download url")

    def list_files(
        self,
        owner: str,
        limit: int = 25,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        page_size = max(1, min(int(limit), MAX_PAGE_SIZE))
        kwargs: Dict[str, Any] = {
            "IndexName": self.owner_index,
            "KeyConditionExpression": Key("owner").eq(owner),
            "Limit": page_size,
            "ScanIndexForward": False,
        }
        if next_token:
            kwargs["ExclusiveStartKey"] = decode_token(next_token)
        try:
            response = self.table.query(**kwargs)
        except Exception as exc:
            raise _wrap(exc, "failed to list files")
        items = [_clean(row) for row in (response or {}).get("Items", [])]
        last_key = (response or {}).get("LastEvaluatedKey")
        return items, (encode_token(last_key) if last_key else None)

    def delete_file(self, file_id: str) -> Dict[str, Any]:
        item = self.get_file(file_id)
        s3_key = str(item.get("s3_key", ""))
        if s3_key:
            try:
                self.s3.delete_object(Bucket=self.bucket, Key=s3_key)
            except Exception as exc:
                code = _error_code(exc)
                if code not in NOT_FOUND_CODES:
                    raise _wrap(exc, "failed to delete object")
                LOGGER.warning("s3 object already absent key=%s", s3_key)
        try:
            self.table.delete_item(Key={"file_id": file_id})
        except Exception as exc:
            raise _wrap(exc, "failed to delete metadata")
        return item

    def _owner_rows(self, owner: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        start_key: Optional[Dict[str, Any]] = None
        while True:
            kwargs: Dict[str, Any] = {
                "IndexName": self.owner_index,
                "KeyConditionExpression": Key("owner").eq(owner),
                "ProjectionExpression": "#o, size_bytes",
                "ExpressionAttributeNames": {"#o": "owner"},
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            try:
                response = self.table.query(**kwargs)
            except Exception as exc:
                raise _wrap(exc, "failed to compute usage")
            rows.extend(_clean(row) for row in (response or {}).get("Items", []))
            start_key = (response or {}).get("LastEvaluatedKey")
            if not start_key:
                return rows

    def _all_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        start_key: Optional[Dict[str, Any]] = None
        while True:
            kwargs: Dict[str, Any] = {
                "ProjectionExpression": "#o, size_bytes",
                "ExpressionAttributeNames": {"#o": "owner"},
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            try:
                response = self.table.scan(**kwargs)
            except Exception as exc:
                raise _wrap(exc, "failed to compute usage")
            rows.extend(_clean(row) for row in (response or {}).get("Items", []))
            start_key = (response or {}).get("LastEvaluatedKey")
            if not start_key:
                return rows

    def usage(self, owner: Optional[str] = None) -> Tuple[List[Dict[str, Any]], int]:
        if owner:
            rows = self._owner_rows(owner)
            totals: Dict[str, List[int]] = {owner: [0, 0]}
        else:
            rows = self._all_rows()
            totals = {}
        for row in rows:
            key = str(row.get("owner", "unknown"))
            entry = totals.setdefault(key, [0, 0])
            entry[0] += 1
            entry[1] += int(row.get("size_bytes", 0) or 0)
        owners = [
            {"owner": name, "file_count": counts[0], "total_bytes": counts[1]}
            for name, counts in sorted(totals.items())
        ]
        total_bytes = sum(entry["total_bytes"] for entry in owners)
        return owners, total_bytes
