"""Data access layer for the document_store backend.

Two interchangeable repository implementations are provided:

* :class:`AwsDocumentRepository` - talks to S3 and DynamoDB through boto3.
* :class:`InMemoryDocumentRepository` - pure Python, used by the test suite and
  for local experiments without AWS/LocalStack.
"""

import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key

DEFAULT_BUCKET = "document-store-documents"
DEFAULT_METADATA_TABLE = "document-metadata"
DEFAULT_TAG_TABLE = "document-tag-index"
DEFAULT_PRESIGN_EXPIRY = 900
MAX_PRESIGN_EXPIRY = 3600

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class DocumentNotFoundError(Exception):
    """Raised when a document (or one of its versions) does not exist."""


def aws_region() -> str:
    """Region used for every AWS client; defaults to us-east-1."""
    return os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def aws_endpoint_url() -> Optional[str]:
    """Optional custom endpoint (LocalStack) taken from AWS_ENDPOINT_URL."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def s3_client():
    """Create an S3 client honouring AWS_ENDPOINT_URL."""
    return boto3.client(
        "s3",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def dynamodb_resource():
    """Create a DynamoDB resource honouring AWS_ENDPOINT_URL."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def bucket_name() -> str:
    return os.environ.get("DOCUMENTS_BUCKET", DEFAULT_BUCKET)


def metadata_table_name() -> str:
    return os.environ.get("METADATA_TABLE", DEFAULT_METADATA_TABLE)


def tag_table_name() -> str:
    return os.environ.get("TAG_INDEX_TABLE", DEFAULT_TAG_TABLE)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_after(seconds: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=int(seconds))
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def md5_hex(data: bytes) -> str:
    """Content checksum (not used for any security decision)."""
    try:
        digest = hashlib.md5(data, usedforsecurity=False)  # nosec B324 - integrity checksum only
    except TypeError:  # pragma: no cover - very old interpreters
        digest = hashlib.md5(data)  # nosec B324 - integrity checksum only
    return digest.hexdigest()


def sanitise_filename(filename: Optional[str]) -> str:
    raw = (filename or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = _UNSAFE_FILENAME.sub("_", raw).strip("._")
    return cleaned[:180] or "document.bin"


def normalise_tags(raw: Optional[Iterable[str]]) -> List[str]:
    """Lowercase, split comma separated values, de-duplicate and sort tags."""
    tags: List[str] = []
    for entry in raw or []:
        if entry is None:
            continue
        for piece in str(entry).split(","):
            tag = piece.strip().lower()
            if tag and tag not in tags:
                tags.append(tag)
    return sorted(tags)


def _clean(value: Any) -> Any:
    """Convert DynamoDB Decimal/set values into plain JSON friendly Python types."""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_clean(item) for item in value)
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    return value


def summarise(versions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a document summary from its (version ordered) metadata items."""
    first = versions[0]
    latest = versions[-1]
    return {
        "document_id": latest["document_id"],
        "title": latest["title"],
        "author": latest["author"],
        "tags": list(latest.get("tags") or []),
        "latest_version": int(latest["version"]),
        "version_count": len(versions),
        "filename": latest["filename"],
        "content_type": latest["content_type"],
        "size_bytes": int(latest["size_bytes"]),
        "created_at": first["created_at"],
        "updated_at": latest["created_at"],
    }


def build_s3_key(document_id: str, version: int, filename: str) -> str:
    return "documents/{0}/v{1}/{2}".format(document_id, int(version), filename)


class AwsDocumentRepository:
    """Repository backed by a versioned S3 bucket and two DynamoDB tables."""

    def __init__(self, s3=None, dynamodb=None, bucket=None, metadata_table=None, tag_table=None):
        self._s3 = s3 if s3 is not None else s3_client()
        self._dynamodb = dynamodb if dynamodb is not None else dynamodb_resource()
        self.bucket = bucket or bucket_name()
        self.metadata_table_name = metadata_table or metadata_table_name()
        self.tag_table_name = tag_table or tag_table_name()

    # -- infrastructure helpers -------------------------------------------------
    def _metadata_table(self):
        return self._dynamodb.Table(self.metadata_table_name)

    def _tag_table(self):
        return self._dynamodb.Table(self.tag_table_name)

    def _check_bucket(self) -> str:
        try:
            self._s3.head_bucket(Bucket=self.bucket)
            return "ok"
        except Exception as exc:  # pragma: no cover - depends on live AWS state
            return "error: {0}".format(exc)

    def _check_table(self, name: str) -> str:
        try:
            self._dynamodb.Table(name).load()
            return "ok"
        except Exception as exc:  # pragma: no cover - depends on live AWS state
            return "error: {0}".format(exc)

    def health(self) -> Dict[str, Any]:
        checks = {
            "s3": self._check_bucket(),
            "metadata_table": self._check_table(self.metadata_table_name),
            "tag_index_table": self._check_table(self.tag_table_name),
        }
        status = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
        return {"status": status, "checks": checks, "bucket": self.bucket, "region": aws_region()}

    # -- writes -----------------------------------------------------------------
    def create_document(self, title, author, tags, filename, content_type, data) -> Dict[str, Any]:
        document_id = str(uuid.uuid4())
        return self._store_version(
            document_id=document_id,
            version=1,
            title=title,
            author=author,
            tags=tags,
            filename=filename,
            content_type=content_type,
            data=data,
            previous_tags=[],
        )

    def add_version(self, document_id, filename, content_type, data, title=None, author=None,
                    tags=None) -> Dict[str, Any]:
        versions = self.list_versions(document_id)
        if not versions:
            raise DocumentNotFoundError("document {0} not found".format(document_id))
        latest = versions[-1]
        previous_tags = list(latest.get("tags") or [])
        return self._store_version(
            document_id=document_id,
            version=int(latest["version"]) + 1,
            title=title or latest["title"],
            author=author or latest["author"],
            tags=tags if tags else previous_tags,
            filename=filename or latest["filename"],
            content_type=content_type or latest["content_type"],
            data=data,
            previous_tags=previous_tags,
        )

    def _store_version(self, document_id, version, title, author, tags, filename, content_type, data,
                       previous_tags) -> Dict[str, Any]:
        clean_tags = normalise_tags(tags)
        safe_name = sanitise_filename(filename)
        key = build_s3_key(document_id, version, safe_name)
        response = self._s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )
        created_at = utc_now_iso()
        item = {
            "document_id": document_id,
            "version": int(version),
            "title": title,
            "author": author,
            "tags": list(clean_tags),
            "filename": safe_name,
            "content_type": content_type or "application/octet-stream",
            "size_bytes": len(data),
            "s3_key": key,
            "s3_version_id": (response or {}).get("VersionId") or "null",
            "checksum_md5": md5_hex(data),
            "created_at": created_at,
        }
        self._metadata_table().put_item(Item=item)
        self._sync_tags(
            document_id=document_id,
            tags=clean_tags,
            previous_tags=previous_tags,
            title=title,
            author=author,
            version=int(version),
            updated_at=created_at,
        )
        return dict(item)

    def _sync_tags(self, document_id, tags, previous_tags, title, author, version, updated_at) -> None:
        table = self._tag_table()
        for stale in sorted(set(previous_tags or []) - set(tags)):
            table.delete_item(Key={"tag": stale, "document_id": document_id})
        for tag in tags:
            table.put_item(
                Item={
                    "tag": tag,
                    "document_id": document_id,
                    "title": title,
                    "author": author,
                    "latest_version": int(version),
                    "updated_at": updated_at,
                }
            )

    # -- reads ------------------------------------------------------------------
    def list_versions(self, document_id: str) -> List[Dict[str, Any]]:
        table = self._metadata_table()
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("document_id").eq(document_id),
            "ScanIndexForward": True,
        }
        items: List[Dict[str, Any]] = []
        while True:
            response = table.query(**kwargs)
            items.extend(response.get("Items") or [])
            start = response.get("LastEvaluatedKey")
            if not start:
                break
            kwargs["ExclusiveStartKey"] = start
        cleaned = [_clean(item) for item in items]
        cleaned.sort(key=lambda entry: int(entry.get("version", 0)))
        return cleaned

    def get_version(self, document_id: str, version: int) -> Dict[str, Any]:
        response = self._metadata_table().get_item(
            Key={"document_id": document_id, "version": int(version)}
        )
        item = (response or {}).get("Item")
        if not item:
            raise DocumentNotFoundError(
                "version {0} of document {1} not found".format(version, document_id)
            )
        return _clean(item)

    def get_document(self, document_id: str) -> Dict[str, Any]:
        versions = self.list_versions(document_id)
        if not versions:
            raise DocumentNotFoundError("document {0} not found".format(document_id))
        return summarise(versions)

    def list_documents(self, limit: int = 20, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
        table = self._metadata_table()
        kwargs: Dict[str, Any] = {}
        items: List[Dict[str, Any]] = []
        while True:
            response = table.scan(**kwargs)
            items.extend(response.get("Items") or [])
            start = response.get("LastEvaluatedKey")
            if not start:
                break
            kwargs["ExclusiveStartKey"] = start
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for raw in items:
            item = _clean(raw)
            grouped.setdefault(item["document_id"], []).append(item)
        summaries = []
        for versions in grouped.values():
            versions.sort(key=lambda entry: int(entry.get("version", 0)))
            summaries.append(summarise(versions))
        summaries.sort(key=lambda entry: (entry["updated_at"], entry["document_id"]), reverse=True)
        return summaries[offset:offset + int(limit)], len(summaries)

    def search_by_tag(self, tag: str, limit: int = 20) -> List[Dict[str, Any]]:
        response = self._tag_table().query(
            KeyConditionExpression=Key("tag").eq(tag),
            Limit=int(limit),
        )
        return [_clean(item) for item in (response.get("Items") or [])]

    def presigned_url(self, document_id: str, version: int, expires_in: int = DEFAULT_PRESIGN_EXPIRY):
        expires_in = max(1, min(int(expires_in), MAX_PRESIGN_EXPIRY))
        item = self.get_version(document_id, version)
        params: Dict[str, Any] = {"Bucket": self.bucket, "Key": item["s3_key"]}
        version_id = item.get("s3_version_id")
        if version_id and version_id != "null":
            params["VersionId"] = version_id
        url = self._s3.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)
        return {
            "document_id": document_id,
            "version": int(item["version"]),
            "url": url,
            "expires_in_seconds": expires_in,
            "expires_at": iso_after(expires_in),
            "filename": item["filename"],
            "s3_version_id": version_id,
        }

    # -- deletes ----------------------------------------------------------------
    def delete_document(self, document_id: str) -> int:
        versions = self.list_versions(document_id)
        if not versions:
            raise DocumentNotFoundError("document {0} not found".format(document_id))
        table = self._metadata_table()
        tags = set()
        for item in versions:
            params: Dict[str, Any] = {"Bucket": self.bucket, "Key": item["s3_key"]}
            version_id = item.get("s3_version_id")
            if version_id and version_id != "null":
                params["VersionId"] = version_id
            self._s3.delete_object(**params)
            table.delete_item(Key={"document_id": document_id, "version": int(item["version"])})
            for tag in item.get("tags") or []:
                tags.add(tag)
        tag_table = self._tag_table()
        for tag in sorted(tags):
            tag_table.delete_item(Key={"tag": tag, "document_id": document_id})
        return len(versions)


class InMemoryDocumentRepository:
    """In-process repository with the same interface as the AWS one (tests/dev)."""

    def __init__(self, bucket: str = "in-memory-bucket"):
        self.bucket = bucket
        self._versions: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self._tags: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._blobs: Dict[str, bytes] = {}

    def health(self) -> Dict[str, Any]:
        checks = {"s3": "ok", "metadata_table": "ok", "tag_index_table": "ok"}
        return {"status": "ok", "checks": checks, "bucket": self.bucket, "region": aws_region()}

    def create_document(self, title, author, tags, filename, content_type, data) -> Dict[str, Any]:
        document_id = str(uuid.uuid4())
        return self._store(document_id, 1, title, author, tags, filename, content_type, data, [])

    def add_version(self, document_id, filename, content_type, data, title=None, author=None,
                    tags=None) -> Dict[str, Any]:
        existing = self._versions.get(document_id)
        if not existing:
            raise DocumentNotFoundError("document {0} not found".format(document_id))
        latest = existing[max(existing)]
        previous_tags = list(latest.get("tags") or [])
        return self._store(
            document_id,
            int(latest["version"]) + 1,
            title or latest["title"],
            author or latest["author"],
            tags if tags else previous_tags,
            filename or latest["filename"],
            content_type or latest["content_type"],
            data,
            previous_tags,
        )

    def _store(self, document_id, version, title, author, tags, filename, content_type, data,
               previous_tags) -> Dict[str, Any]:
        clean_tags = normalise_tags(tags)
        safe_name = sanitise_filename(filename)
        key = build_s3_key(document_id, version, safe_name)
        self._blobs[key] = bytes(data)
        created_at = utc_now_iso()
        item = {
            "document_id": document_id,
            "version": int(version),
            "title": title,
            "author": author,
            "tags": list(clean_tags),
            "filename": safe_name,
            "content_type": content_type or "application/octet-stream",
            "size_bytes": len(data),
            "s3_key": key,
            "s3_version_id": "mem-{0}-{1}".format(int(version), uuid.uuid4().hex[:8]),
            "checksum_md5": md5_hex(bytes(data)),
            "created_at": created_at,
        }
        self._versions.setdefault(document_id, {})[int(version)] = item
        for stale in set(previous_tags or []) - set(clean_tags):
            entries = self._tags.get(stale)
            if entries:
                entries.pop(document_id, None)
        for tag in clean_tags:
            self._tags.setdefault(tag, {})[document_id] = {
                "tag": tag,
                "document_id": document_id,
                "title": title,
                "author": author,
                "latest_version": int(version),
                "updated_at": created_at,
            }
        return dict(item)

    def list_versions(self, document_id: str) -> List[Dict[str, Any]]:
        versions = self._versions.get(document_id) or {}
        return [dict(versions[key]) for key in sorted(versions)]

    def get_version(self, document_id: str, version: int) -> Dict[str, Any]:
        versions = self._versions.get(document_id) or {}
        item = versions.get(int(version))
        if not item:
            raise DocumentNotFoundError(
                "version {0} of document {1} not found".format(version, document_id)
            )
        return dict(item)

    def get_document(self, document_id: str) -> Dict[str, Any]:
        versions = self.list_versions(document_id)
        if not versions:
            raise DocumentNotFoundError("document {0} not found".format(document_id))
        return summarise(versions)

    def list_documents(self, limit: int = 20, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
        summaries = []
        for versions in self._versions.values():
            ordered = [versions[key] for key in sorted(versions)]
            summaries.append(summarise(ordered))
        summaries.sort(key=lambda entry: (entry["updated_at"], entry["document_id"]), reverse=True)
        return summaries[offset:offset + int(limit)], len(summaries)

    def search_by_tag(self, tag: str, limit: int = 20) -> List[Dict[str, Any]]:
        entries = list((self._tags.get(tag) or {}).values())
        entries.sort(key=lambda entry: entry["document_id"])
        return [dict(entry) for entry in entries[:int(limit)]]

    def presigned_url(self, document_id: str, version: int, expires_in: int = DEFAULT_PRESIGN_EXPIRY):
        expires_in = max(1, min(int(expires_in), MAX_PRESIGN_EXPIRY))
        item = self.get_version(document_id, version)
        url = "https://{0}.s3.local/{1}?X-Amz-Expires={2}&X-Amz-Version-Id={3}".format(
            self.bucket, item["s3_key"], expires_in, item["s3_version_id"]
        )
        return {
            "document_id": document_id,
            "version": int(item["version"]),
            "url": url,
            "expires_in_seconds": expires_in,
            "expires_at": iso_after(expires_in),
            "filename": item["filename"],
            "s3_version_id": item["s3_version_id"],
        }

    def delete_document(self, document_id: str) -> int:
        versions = self._versions.pop(document_id, None)
        if not versions:
            raise DocumentNotFoundError("document {0} not found".format(document_id))
        for item in versions.values():
            self._blobs.pop(item["s3_key"], None)
            for tag in item.get("tags") or []:
                entries = self._tags.get(tag)
                if entries:
                    entries.pop(document_id, None)
        return len(versions)
