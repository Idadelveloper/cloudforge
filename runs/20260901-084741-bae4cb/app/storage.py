"""Data access layer for the document_store service.

The module exposes a small repository interface with two implementations:

* :class:`AwsDocumentRepository` - S3 (versioned bucket) + DynamoDB via boto3.
* :class:`InMemoryDocumentRepository` - dependency-free implementation used for
  local development and offline tests.

All AWS clients honour ``AWS_ENDPOINT_URL`` so the service works against
LocalStack, and default to the ``us-east-1`` region.
"""

import base64
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key

LOGGER = logging.getLogger("document_store.storage")

DEFAULT_REGION = "us-east-1"
DEFAULT_BUCKET = "document-store-documents"
DEFAULT_TABLE = "documents-metadata"
DEFAULT_TAG_INDEX = "tag-index"
DEFAULT_AUTHOR_INDEX = "author-index"
DEFAULT_SECRET_NAME = "document-store/app-config"
DEFAULT_PRESIGN_EXPIRY = 900
MAX_PRESIGN_EXPIRY = 3600
DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
OBJECT_PREFIX = "documents/"


def aws_region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or DEFAULT_REGION


def aws_endpoint_url() -> Optional[str]:
    return os.environ.get("AWS_ENDPOINT_URL") or None


def s3_client():
    return boto3.client("s3", region_name=aws_region(), endpoint_url=aws_endpoint_url())


def dynamodb_resource():
    return boto3.resource("dynamodb", region_name=aws_region(), endpoint_url=aws_endpoint_url())


def secretsmanager_client():
    return boto3.client("secretsmanager", region_name=aws_region(), endpoint_url=aws_endpoint_url())


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("invalid integer for %s (%r); using default %s", name, raw, default)
        return default
    return value if value > 0 else default


def _secret_lookup_enabled() -> bool:
    """Only talk to Secrets Manager when explicitly enabled or an endpoint is set."""
    flag = os.environ.get("LOAD_APP_CONFIG_SECRET")
    if flag is not None:
        return flag.strip().lower() in ("1", "true", "yes", "on")
    return bool(os.environ.get("AWS_ENDPOINT_URL"))


def _load_api_key(secret_name: str) -> Optional[str]:
    env_value = os.environ.get("DOCUMENT_STORE_API_KEY")
    if env_value:
        return env_value
    if not _secret_lookup_enabled():
        LOGGER.info("no API key configured; write endpoints are unauthenticated")
        return None
    try:
        client = secretsmanager_client()
        raw = client.get_secret_value(SecretId=secret_name).get("SecretString") or "{}"
        payload = json.loads(raw)
        if isinstance(payload, dict):
            value = payload.get("api_key")
            return str(value) if value else None
    except Exception as exc:  # pragma: no cover - depends on live AWS/LocalStack
        LOGGER.warning("could not read app config secret %s: %s", secret_name, exc)
    return None


class Settings:
    """Runtime configuration resolved from environment variables / secrets."""

    def __init__(self) -> None:
        self.bucket_name = os.environ.get("DOCUMENTS_BUCKET", DEFAULT_BUCKET)
        self.table_name = os.environ.get("DOCUMENTS_TABLE", DEFAULT_TABLE)
        self.tag_index_name = os.environ.get("TAG_INDEX_NAME", DEFAULT_TAG_INDEX)
        self.author_index_name = os.environ.get("AUTHOR_INDEX_NAME", DEFAULT_AUTHOR_INDEX)
        self.secret_name = os.environ.get("APP_CONFIG_SECRET_NAME", DEFAULT_SECRET_NAME)
        self.default_expiry = _int_env("PRESIGN_DEFAULT_EXPIRY", DEFAULT_PRESIGN_EXPIRY)
        self.max_expiry = _int_env("PRESIGN_MAX_EXPIRY", MAX_PRESIGN_EXPIRY)
        self.max_upload_bytes = _int_env("MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
        self.api_key = _load_api_key(self.secret_name)


_SETTINGS: Optional[Settings] = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings()
    return _SETTINGS


def reset_settings() -> None:
    """Drop the cached settings (used by tests)."""
    global _SETTINGS
    _SETTINGS = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_tags(raw: Any) -> List[str]:
    """Accept a comma-separated string or a sequence and return clean lowercase tags."""
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates: List[str] = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        candidates = [str(item) for item in raw]
    else:
        candidates = [str(raw)]
    tags: List[str] = []
    for candidate in candidates:
        cleaned = candidate.strip().lower()
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tags


def to_jsonable(value: Any) -> Any:
    """Convert DynamoDB types (Decimal, sets, bytes) into JSON-friendly values."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return value


def encode_token(key: Optional[Dict[str, Any]]) -> Optional[str]:
    if not key:
        return None
    raw = json.dumps(to_jsonable(key), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ValueError("invalid next_token")
    if not isinstance(decoded, dict):
        raise ValueError("invalid next_token")
    return decoded


def object_key_for(document_id: str) -> str:
    """Stable S3 key per document so bucket versioning tracks each upload."""
    return "%s%s" % (OBJECT_PREFIX, document_id)


def build_metadata_item(
    document_id: str,
    version: int,
    title: str,
    author: str,
    tags: Any,
    s3_key: str,
    s3_version_id: Optional[str],
    filename: str,
    content_type: str,
    data: bytes,
) -> Dict[str, Any]:
    tag_list = normalize_tags(tags)
    return {
        "document_id": document_id,
        "version": int(version),
        "title": title,
        "author": author,
        "tags": tag_list,
        "tag": tag_list[0] if tag_list else "untagged",
        "s3_key": s3_key,
        "s3_version_id": s3_version_id or "null",
        "filename": filename,
        "content_type": content_type or "application/octet-stream",
        "size_bytes": len(data),
        "checksum": hashlib.sha256(data).hexdigest(),
        "created_at": now_iso(),
        "is_latest": True,
    }


def presigned_payload(document_id: str, version: int, url: str, expires_in: int) -> Dict[str, Any]:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    return {
        "document_id": document_id,
        "version": int(version),
        "url": url,
        "expires_in_seconds": int(expires_in),
        "expires_at": expires_at.isoformat(),
    }


class DocumentRepository:
    """Interface implemented by the AWS and in-memory repositories."""

    def health(self) -> Dict[str, Any]:
        raise NotImplementedError

    def create_document(
        self,
        title: str,
        author: str,
        tags: Any,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def add_version(
        self,
        document_id: str,
        title: Optional[str],
        author: Optional[str],
        tags: Any,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def list_documents(
        self,
        author: Optional[str] = None,
        limit: int = 25,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        raise NotImplementedError

    def list_versions(self, document_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def get_version(self, document_id: str, version: int) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def create_presigned_url(self, document_id: str, version: int, expires_in: int) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def search_by_tag(self, tag: str, limit: int = 25) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def delete_document(self, document_id: str) -> int:
        raise NotImplementedError


class AwsDocumentRepository(DocumentRepository):
    """S3 + DynamoDB backed repository."""

    def __init__(self, settings: Optional[Settings] = None, s3: Any = None, table: Any = None) -> None:
        self.settings = settings or get_settings()
        self._s3 = s3
        self._table = table

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            self._s3 = s3_client()
        return self._s3

    @property
    def table(self) -> Any:
        if self._table is None:
            self._table = dynamodb_resource().Table(self.settings.table_name)
        return self._table

    def health(self) -> Dict[str, Any]:
        dependencies: Dict[str, str] = {}
        healthy = True
        try:
            self.s3.head_bucket(Bucket=self.settings.bucket_name)
            dependencies["s3"] = "ok"
        except Exception as exc:
            healthy = False
            dependencies["s3"] = "error: %s" % type(exc).__name__
            LOGGER.warning("s3 health check failed: %s", exc)
        try:
            self.table.load()
            dependencies["dynamodb"] = "ok"
        except Exception as exc:
            healthy = False
            dependencies["dynamodb"] = "error: %s" % type(exc).__name__
            LOGGER.warning("dynamodb health check failed: %s", exc)
        return {"healthy": healthy, "dependencies": dependencies}

    def create_document(
        self,
        title: str,
        author: str,
        tags: Any,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> Dict[str, Any]:
        document_id = str(uuid.uuid4())
        return self._write_version(document_id, 1, title, author, tags, filename, content_type, data)

    def add_version(
        self,
        document_id: str,
        title: Optional[str],
        author: Optional[str],
        tags: Any,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> Optional[Dict[str, Any]]:
        latest = self._latest_item(document_id)
        if latest is None:
            return None
        previous_version = int(latest.get("version", 0))
        new_version = previous_version + 1
        new_tags = tags if tags else [str(tag) for tag in (latest.get("tags") or [])]
        item = self._write_version(
            document_id,
            new_version,
            title or str(latest.get("title", "")),
            author or str(latest.get("author", "")),
            new_tags,
            filename,
            content_type,
            data,
        )
        self._clear_latest(document_id, previous_version)
        return item

    def list_documents(
        self,
        author: Optional[str] = None,
        limit: int = 25,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        start_key = decode_token(next_token)
        kwargs: Dict[str, Any] = {"Limit": int(limit), "FilterExpression": Attr("is_latest").eq(True)}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        if author:
            kwargs["IndexName"] = self.settings.author_index_name
            kwargs["KeyConditionExpression"] = Key("author").eq(author)
            response = self.table.query(**kwargs)
        else:
            response = self.table.scan(**kwargs)
        items = [to_jsonable(item) for item in response.get("Items", [])]
        return items, encode_token(response.get("LastEvaluatedKey"))

    def list_versions(self, document_id: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("document_id").eq(document_id)}
        while True:
            response = self.table.query(**kwargs)
            items.extend(to_jsonable(item) for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
            kwargs["ExclusiveStartKey"] = start_key
        items.sort(key=lambda item: int(item.get("version", 0)))
        return items

    def get_version(self, document_id: str, version: int) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key={"document_id": document_id, "version": int(version)})
        item = response.get("Item")
        return to_jsonable(item) if item else None

    def create_presigned_url(self, document_id: str, version: int, expires_in: int) -> Optional[Dict[str, Any]]:
        item = self.get_version(document_id, version)
        if item is None:
            return None
        params: Dict[str, Any] = {
            "Bucket": self.settings.bucket_name,
            "Key": str(item.get("s3_key") or object_key_for(document_id)),
        }
        s3_version_id = item.get("s3_version_id")
        if s3_version_id and s3_version_id != "null":
            params["VersionId"] = str(s3_version_id)
        url = self.s3.generate_presigned_url("get_object", Params=params, ExpiresIn=int(expires_in))
        return presigned_payload(document_id, int(version), str(url), int(expires_in))

    def search_by_tag(self, tag: str, limit: int = 25) -> List[Dict[str, Any]]:
        normalized = tag.strip().lower()
        items: List[Dict[str, Any]] = []
        try:
            response = self.table.query(
                IndexName=self.settings.tag_index_name,
                KeyConditionExpression=Key("tag").eq(normalized),
                ScanIndexForward=False,
                Limit=int(limit),
            )
            items = [to_jsonable(item) for item in response.get("Items", [])]
        except Exception as exc:
            LOGGER.warning("tag index query failed (%s); falling back to scan", exc)
        if not items:
            response = self.table.scan(
                FilterExpression=Attr("tags").contains(normalized),
                Limit=int(limit),
            )
            items = [to_jsonable(item) for item in response.get("Items", [])]
        return items[: int(limit)]

    def delete_document(self, document_id: str) -> int:
        versions = self.list_versions(document_id)
        if not versions:
            return 0
        self._delete_object_versions(object_key_for(document_id))
        for item in versions:
            self.table.delete_item(
                Key={"document_id": document_id, "version": int(item.get("version", 0))}
            )
        return len(versions)

    def _write_version(
        self,
        document_id: str,
        version: int,
        title: str,
        author: str,
        tags: Any,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> Dict[str, Any]:
        key = object_key_for(document_id)
        response = self.s3.put_object(
            Bucket=self.settings.bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
            Metadata={"document-id": document_id, "version": str(version)},
        )
        item = build_metadata_item(
            document_id,
            version,
            title,
            author,
            tags,
            key,
            response.get("VersionId"),
            filename,
            content_type,
            data,
        )
        self.table.put_item(Item=item)
        return to_jsonable(item)

    def _latest_item(self, document_id: str) -> Optional[Dict[str, Any]]:
        response = self.table.query(
            KeyConditionExpression=Key("document_id").eq(document_id),
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        return to_jsonable(items[0]) if items else None

    def _clear_latest(self, document_id: str, version: int) -> None:
        try:
            self.table.update_item(
                Key={"document_id": document_id, "version": int(version)},
                UpdateExpression="SET is_latest = :flag",
                ExpressionAttributeValues={":flag": False},
            )
        except Exception as exc:
            LOGGER.warning("could not clear is_latest for %s v%s: %s", document_id, version, exc)

    def _delete_object_versions(self, key: str) -> None:
        try:
            response = self.s3.list_object_versions(Bucket=self.settings.bucket_name, Prefix=key)
        except Exception as exc:
            LOGGER.warning("could not list object versions for %s: %s", key, exc)
            return
        entries = list(response.get("Versions", [])) + list(response.get("DeleteMarkers", []))
        for entry in entries:
            if entry.get("Key") != key:
                continue
            try:
                self.s3.delete_object(
                    Bucket=self.settings.bucket_name,
                    Key=key,
                    VersionId=entry.get("VersionId", "null"),
                )
            except Exception as exc:
                LOGGER.warning("could not delete %s version %s: %s", key, entry.get("VersionId"), exc)


class InMemoryDocumentRepository(DocumentRepository):
    """Dependency-free repository used for offline tests and local runs."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._docs: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self._blobs: Dict[str, bytes] = {}

    def health(self) -> Dict[str, Any]:
        return {"healthy": True, "dependencies": {"s3": "in-memory", "dynamodb": "in-memory"}}

    def create_document(
        self,
        title: str,
        author: str,
        tags: Any,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> Dict[str, Any]:
        document_id = str(uuid.uuid4())
        return self._store(document_id, 1, title, author, tags, filename, content_type, data)

    def add_version(
        self,
        document_id: str,
        title: Optional[str],
        author: Optional[str],
        tags: Any,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> Optional[Dict[str, Any]]:
        versions = self._docs.get(document_id)
        if not versions:
            return None
        previous_version = max(versions)
        previous = versions[previous_version]
        previous["is_latest"] = False
        new_tags = tags if tags else list(previous.get("tags") or [])
        return self._store(
            document_id,
            previous_version + 1,
            title or str(previous.get("title", "")),
            author or str(previous.get("author", "")),
            new_tags,
            filename,
            content_type,
            data,
        )

    def list_documents(
        self,
        author: Optional[str] = None,
        limit: int = 25,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        key = decode_token(next_token)
        start = int(key.get("offset", 0)) if key else 0
        items = [
            dict(item)
            for versions in self._docs.values()
            for item in versions.values()
            if item.get("is_latest")
        ]
        if author:
            items = [item for item in items if item.get("author") == author]
        items.sort(key=lambda item: str(item.get("created_at")), reverse=True)
        page = items[start:start + int(limit)]
        token = None
        if start + int(limit) < len(items):
            token = encode_token({"offset": start + int(limit)})
        return page, token

    def list_versions(self, document_id: str) -> List[Dict[str, Any]]:
        versions = self._docs.get(document_id) or {}
        return [dict(versions[key]) for key in sorted(versions)]

    def get_version(self, document_id: str, version: int) -> Optional[Dict[str, Any]]:
        item = (self._docs.get(document_id) or {}).get(int(version))
        return dict(item) if item else None

    def create_presigned_url(self, document_id: str, version: int, expires_in: int) -> Optional[Dict[str, Any]]:
        item = self.get_version(document_id, version)
        if item is None:
            return None
        url = "https://in-memory.invalid/%s/%s?versionId=%s&X-Amz-Expires=%d" % (
            self.settings.bucket_name,
            item.get("s3_key"),
            item.get("s3_version_id"),
            int(expires_in),
        )
        return presigned_payload(document_id, int(version), url, int(expires_in))

    def search_by_tag(self, tag: str, limit: int = 25) -> List[Dict[str, Any]]:
        normalized = tag.strip().lower()
        matches = [
            dict(item)
            for versions in self._docs.values()
            for item in versions.values()
            if normalized in (item.get("tags") or [])
        ]
        matches.sort(key=lambda item: str(item.get("created_at")), reverse=True)
        return matches[: int(limit)]

    def delete_document(self, document_id: str) -> int:
        versions = self._docs.pop(document_id, None)
        if not versions:
            return 0
        for version in versions:
            self._blobs.pop("%s:%d" % (document_id, version), None)
        return len(versions)

    def _store(
        self,
        document_id: str,
        version: int,
        title: str,
        author: str,
        tags: Any,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> Dict[str, Any]:
        item = build_metadata_item(
            document_id,
            version,
            title,
            author,
            tags,
            object_key_for(document_id),
            "mem-v%d" % version,
            filename,
            content_type,
            data,
        )
        self._docs.setdefault(document_id, {})[int(version)] = item
        self._blobs["%s:%d" % (document_id, version)] = bytes(data)
        return dict(item)
