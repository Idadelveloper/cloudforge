"""Data access layer: S3 object storage and DynamoDB metadata storage.

All AWS clients honour AWS_ENDPOINT_URL (LocalStack) and default to us-east-1.
In-memory implementations are provided for local development and tests.
"""
import base64
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger("file_share_backend.storage")

DEFAULT_REGION = "us-east-1"
DEFAULT_BUCKET = "file-share-files"
DEFAULT_TABLE = "file-share-metadata"
DEFAULT_OWNER_INDEX = "owner-index"
DEFAULT_EXPIRY_SECONDS = 900
MIN_EXPIRY_SECONDS = 60
MAX_EXPIRY_SECONDS = 604800
NOT_FOUND_CODES = {"404", "NotFound", "NoSuchKey", "NoSuchBucket", "ResourceNotFoundException"}


def region_name() -> str:
    """AWS region used by every client."""
    return os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)


def endpoint_url() -> Optional[str]:
    """Custom AWS endpoint (LocalStack) if configured."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def bucket_name() -> str:
    """Name of the S3 bucket holding uploaded objects."""
    return os.environ.get("FILE_SHARE_BUCKET", DEFAULT_BUCKET)


def table_name() -> str:
    """Name of the DynamoDB metadata table."""
    return os.environ.get("FILE_SHARE_TABLE", DEFAULT_TABLE)


def owner_index_name() -> str:
    """Name of the owner GSI on the metadata table."""
    return os.environ.get("FILE_SHARE_OWNER_INDEX", DEFAULT_OWNER_INDEX)


def presign_expiry_seconds() -> int:
    """Lifetime of generated presigned URLs, in seconds."""
    raw = os.environ.get("PRESIGNED_URL_EXPIRY_SECONDS", str(DEFAULT_EXPIRY_SECONDS))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_EXPIRY_SECONDS
    return max(MIN_EXPIRY_SECONDS, min(value, MAX_EXPIRY_SECONDS))


def s3_client() -> Any:
    """Create an S3 client pointed at the configured endpoint."""
    return boto3.client("s3", region_name=region_name(), endpoint_url=endpoint_url())


def dynamodb_resource() -> Any:
    """Create a DynamoDB resource pointed at the configured endpoint."""
    return boto3.resource("dynamodb", region_name=region_name(), endpoint_url=endpoint_url())


def error_code(exc: Exception) -> str:
    """Extract the AWS error code from a botocore ClientError-like exception."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def encode_token(key: Dict[str, Any]) -> str:
    """Encode a DynamoDB LastEvaluatedKey as an opaque pagination token."""
    payload = json.dumps(decode_values(key), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_token(token: str) -> Dict[str, Any]:
    """Decode an opaque pagination token back into a DynamoDB key."""
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid pagination token") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid pagination token")
    return value


def decode_values(value: Any) -> Any:
    """Recursively convert DynamoDB Decimals into plain ints/floats."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {key: decode_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [decode_values(item) for item in value]
    return value


def _clean(item: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


class ObjectStore:
    """Interface for the object storage backend."""

    def healthy(self) -> bool:
        """Return True when the bucket is reachable."""
        raise NotImplementedError

    def presigned_put_url(self, key: str, content_type: str, expires_in: int) -> str:
        """Return a presigned URL a client can PUT the object to."""
        raise NotImplementedError

    def presigned_get_url(self, key: str, expires_in: int, filename: str = "") -> str:
        """Return a presigned URL a client can GET the object from."""
        raise NotImplementedError

    def head_object(self, key: str) -> Optional[Dict[str, Any]]:
        """Return object attributes, or None when the object is missing."""
        raise NotImplementedError

    def delete_object(self, key: str) -> bool:
        """Delete the object; returns True when the call succeeded."""
        raise NotImplementedError


class FileRepository:
    """Interface for the metadata repository."""

    def healthy(self) -> bool:
        """Return True when the metadata store is reachable."""
        raise NotImplementedError

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a new metadata record."""
        raise NotImplementedError

    def get(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a metadata record by id."""
        raise NotImplementedError

    def update(self, file_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply attribute updates to an existing record."""
        raise NotImplementedError

    def delete(self, file_id: str) -> bool:
        """Remove a metadata record."""
        raise NotImplementedError

    def list_by_owner(
        self,
        owner: str,
        limit: int = 50,
        start_key: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Return one page of records for an owner plus the next start key."""
        raise NotImplementedError

    def all_by_owner(self, owner: str) -> List[Dict[str, Any]]:
        """Return every record belonging to an owner."""
        raise NotImplementedError

    def scan_all(self) -> List[Dict[str, Any]]:
        """Return every record in the table."""
        raise NotImplementedError


class S3ObjectStore(ObjectStore):
    """S3-backed object store."""

    def __init__(self, client: Any = None, bucket: Optional[str] = None) -> None:
        self._client = client
        self._bucket = bucket or bucket_name()

    @property
    def client(self) -> Any:
        """Lazily created boto3 S3 client."""
        if self._client is None:
            self._client = s3_client()
        return self._client

    @property
    def bucket(self) -> str:
        """Bucket this store writes to."""
        return self._bucket

    def healthy(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self._bucket)
            return True
        except Exception as exc:  # noqa: BLE001 - health probe must not raise
            logger.warning("s3 health check failed: %s", exc)
            return False

    def presigned_put_url(self, key: str, content_type: str, expires_in: int) -> str:
        return str(
            self.client.generate_presigned_url(
                "put_object",
                Params={"Bucket": self._bucket, "Key": key, "ContentType": content_type},
                ExpiresIn=expires_in,
            )
        )

    def presigned_get_url(self, key: str, expires_in: int, filename: str = "") -> str:
        params: Dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = 'attachment; filename="{0}"'.format(filename)
        return str(
            self.client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=expires_in,
            )
        )

    def head_object(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            return dict(self.client.head_object(Bucket=self._bucket, Key=key))
        except Exception as exc:  # noqa: BLE001 - translate 404 into None
            if error_code(exc) in NOT_FOUND_CODES:
                return None
            raise

    def delete_object(self, key: str) -> bool:
        try:
            self.client.delete_object(Bucket=self._bucket, Key=key)
            return True
        except Exception as exc:  # noqa: BLE001 - deletion is best effort
            if error_code(exc) in NOT_FOUND_CODES:
                return False
            logger.warning("failed to delete s3 object %s: %s", key, exc)
            return False


class DynamoFileRepository(FileRepository):
    """DynamoDB-backed metadata repository."""

    def __init__(self, table: Any = None, index_name: Optional[str] = None) -> None:
        self._table = table
        self._index_name = index_name or owner_index_name()

    @property
    def table(self) -> Any:
        """Lazily created boto3 DynamoDB Table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(table_name())
        return self._table

    def healthy(self) -> bool:
        try:
            status = getattr(self.table, "table_status", None)
            return status is not None
        except Exception as exc:  # noqa: BLE001 - health probe must not raise
            logger.warning("dynamodb health check failed: %s", exc)
            return False

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        self.table.put_item(Item=_clean(item))
        return dict(item)

    def get(self, file_id: str) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key={"file_id": file_id})
        item = response.get("Item") if isinstance(response, dict) else None
        if not item:
            return None
        return decode_values(dict(item))

    def update(self, file_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not updates:
            return self.get(file_id)
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments: List[str] = []
        for index, key in enumerate(sorted(updates)):
            names["#k{0}".format(index)] = key
            values[":v{0}".format(index)] = updates[key]
            assignments.append("#k{0} = :v{0}".format(index))
        response = self.table.update_item(
            Key={"file_id": file_id},
            UpdateExpression="SET " + ", ".join(assignments),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        attributes = response.get("Attributes") if isinstance(response, dict) else None
        if not attributes:
            return None
        return decode_values(dict(attributes))

    def delete(self, file_id: str) -> bool:
        self.table.delete_item(Key={"file_id": file_id})
        return True

    def list_by_owner(
        self,
        owner: str,
        limit: int = 50,
        start_key: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        kwargs: Dict[str, Any] = {
            "IndexName": self._index_name,
            "KeyConditionExpression": Key("owner").eq(owner),
            "Limit": limit,
            "ScanIndexForward": False,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = self.table.query(**kwargs)
        items = [decode_values(dict(item)) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        return items, decode_values(last_key) if last_key else None

    def all_by_owner(self, owner: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        start_key: Optional[Dict[str, Any]] = None
        while True:
            page, start_key = self.list_by_owner(owner, limit=100, start_key=start_key)
            results.extend(page)
            if not start_key:
                return results

    def scan_all(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {}
        while True:
            response = self.table.scan(**kwargs)
            results.extend(decode_values(dict(item)) for item in response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return results
            kwargs["ExclusiveStartKey"] = last_key


class InMemoryObjectStore(ObjectStore):
    """In-memory object store used for local development and tests."""

    def __init__(self, bucket: Optional[str] = None) -> None:
        self.bucket = bucket or bucket_name()
        self.objects: Dict[str, Dict[str, Any]] = {}

    def healthy(self) -> bool:
        return True

    def presigned_put_url(self, key: str, content_type: str, expires_in: int) -> str:
        return "https://{0}.s3.local/{1}?method=PUT&X-Amz-Expires={2}".format(
            self.bucket, quote(key), expires_in
        )

    def presigned_get_url(self, key: str, expires_in: int, filename: str = "") -> str:
        return "https://{0}.s3.local/{1}?method=GET&X-Amz-Expires={2}".format(
            self.bucket, quote(key), expires_in
        )

    def put_object(self, key: str, size_bytes: int, content_type: str = "application/octet-stream") -> None:
        """Simulate a client upload landing in the bucket."""
        self.objects[key] = {
            "ContentLength": int(size_bytes),
            "ContentType": content_type,
            "LastModified": datetime.now(timezone.utc),
        }

    def head_object(self, key: str) -> Optional[Dict[str, Any]]:
        stored = self.objects.get(key)
        return dict(stored) if stored else None

    def delete_object(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None


class InMemoryFileRepository(FileRepository):
    """In-memory metadata repository used for local development and tests."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}

    def healthy(self) -> bool:
        return True

    def create(self, item: Dict[str, Any]) -> Dict[str, Any]:
        self.items[str(item["file_id"])] = dict(item)
        return dict(item)

    def get(self, file_id: str) -> Optional[Dict[str, Any]]:
        stored = self.items.get(file_id)
        return dict(stored) if stored else None

    def update(self, file_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        stored = self.items.get(file_id)
        if stored is None:
            return None
        stored.update(updates)
        return dict(stored)

    def delete(self, file_id: str) -> bool:
        return self.items.pop(file_id, None) is not None

    def list_by_owner(
        self,
        owner: str,
        limit: int = 50,
        start_key: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        rows = sorted(
            (item for item in self.items.values() if item.get("owner") == owner),
            key=lambda row: str(row.get("upload_time", "")),
            reverse=True,
        )
        offset = 0
        if start_key:
            try:
                offset = int(start_key.get("offset", 0))
            except (TypeError, ValueError):
                offset = 0
        page = rows[offset:offset + limit]
        next_offset = offset + limit
        last_key = {"offset": next_offset} if next_offset < len(rows) else None
        return [dict(row) for row in page], last_key

    def all_by_owner(self, owner: str) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.items.values() if item.get("owner") == owner]

    def scan_all(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.items.values()]
