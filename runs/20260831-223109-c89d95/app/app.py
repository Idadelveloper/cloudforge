"""FastAPI file-sharing backend.

Clients request presigned S3 PUT URLs, upload directly to S3, confirm the
upload (metadata recorded in DynamoDB), list/download/delete their files and
query aggregate storage usage per owner.
"""
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from storage import (
    DynamoFileRepository,
    FileRepository,
    ObjectStore,
    S3ObjectStore,
    bucket_name,
    decode_token,
    encode_token,
    presign_expiry_seconds,
    table_name,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("file_share_backend")

DEFAULT_CONTENT_TYPE = "application/octet-stream"
STATUS_PENDING = "pending"
STATUS_AVAILABLE = "available"
MAX_PAGE_SIZE = 200

app = FastAPI(
    title="file_share_backend",
    version="1.0.0",
    description="Presigned-URL file sharing service backed by S3 and DynamoDB.",
)


class HealthResponse(BaseModel):
    """Liveness / dependency status payload."""

    status: str
    s3: bool
    dynamodb: bool
    bucket: str
    table: str


class UploadUrlRequest(BaseModel):
    """Request body for creating a presigned upload URL."""

    owner: str = Field(..., min_length=1, max_length=128)
    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str = Field(default=DEFAULT_CONTENT_TYPE, max_length=255)
    size_bytes: Optional[int] = Field(default=None, ge=0)


class UploadUrlResponse(BaseModel):
    """Presigned PUT URL details."""

    file_id: str
    s3_key: str
    upload_url: str
    expires_in_seconds: int


class DownloadUrlResponse(BaseModel):
    """Presigned GET URL details."""

    file_id: str
    download_url: str
    expires_in_seconds: int


class FileMetadata(BaseModel):
    """Stored metadata for a single file."""

    file_id: str
    owner: str
    filename: str
    content_type: str
    size_bytes: int
    s3_key: str
    status: str
    upload_time: str
    created_at: str


class FileListResponse(BaseModel):
    """Paginated list of files for an owner."""

    owner: str
    items: List[FileMetadata]
    count: int
    next_token: Optional[str] = None


class DeleteResponse(BaseModel):
    """Result of a delete operation."""

    file_id: str
    deleted: bool


class OwnerUsage(BaseModel):
    """Storage usage for one owner."""

    owner: str
    file_count: int
    total_bytes: int


class UsageResponse(BaseModel):
    """Aggregate storage usage."""

    owners: List[OwnerUsage]
    total_bytes: int


_repository: Optional[FileRepository] = None
_object_store: Optional[ObjectStore] = None


def get_repository() -> FileRepository:
    """Return the (lazily created) DynamoDB metadata repository."""
    global _repository
    if _repository is None:
        _repository = DynamoFileRepository()
    return _repository


def get_object_store() -> ObjectStore:
    """Return the (lazily created) S3 object store."""
    global _object_store
    if _object_store is None:
        _object_store = S3ObjectStore()
    return _object_store


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_names(owner: str, filename: str) -> None:
    bad_chars = ("/", "\\", "\x00")
    if any(char in owner for char in bad_chars):
        raise HTTPException(status_code=400, detail="owner must not contain path separators")
    if any(char in filename for char in bad_chars) or filename.strip() in (".", ".."):
        raise HTTPException(status_code=400, detail="filename must not contain path separators")


def _to_model(item: Dict[str, Any]) -> FileMetadata:
    return FileMetadata(
        file_id=str(item.get("file_id", "")),
        owner=str(item.get("owner", "")),
        filename=str(item.get("filename", "")),
        content_type=str(item.get("content_type") or DEFAULT_CONTENT_TYPE),
        size_bytes=int(item.get("size_bytes") or 0),
        s3_key=str(item.get("s3_key", "")),
        status=str(item.get("status") or STATUS_PENDING),
        upload_time=str(item.get("upload_time") or ""),
        created_at=str(item.get("created_at") or ""),
    )


def _usage_for(owner: str, items: List[Dict[str, Any]]) -> OwnerUsage:
    total = 0
    for item in items:
        total += int(item.get("size_bytes") or 0)
    return OwnerUsage(owner=owner, file_count=len(items), total_bytes=total)


@app.get("/health", response_model=HealthResponse)
def health(
    repo: FileRepository = Depends(get_repository),
    store: ObjectStore = Depends(get_object_store),
) -> HealthResponse:
    """Report service status and connectivity to S3 / DynamoDB."""
    s3_ok = store.healthy()
    ddb_ok = repo.healthy()
    return HealthResponse(
        status="ok" if (s3_ok and ddb_ok) else "degraded",
        s3=s3_ok,
        dynamodb=ddb_ok,
        bucket=bucket_name(),
        table=table_name(),
    )


@app.post("/files/upload-url", response_model=UploadUrlResponse, status_code=201)
def create_upload_url(
    payload: UploadUrlRequest,
    repo: FileRepository = Depends(get_repository),
    store: ObjectStore = Depends(get_object_store),
) -> UploadUrlResponse:
    """Create a pending metadata record and return a presigned S3 PUT URL."""
    owner = payload.owner.strip()
    filename = payload.filename.strip()
    if not owner or not filename:
        raise HTTPException(status_code=400, detail="owner and filename are required")
    _validate_names(owner, filename)

    file_id = uuid.uuid4().hex
    s3_key = "{0}/{1}/{2}".format(owner, file_id, filename)
    now = _utc_now()
    content_type = (payload.content_type or DEFAULT_CONTENT_TYPE).strip() or DEFAULT_CONTENT_TYPE
    item = {
        "file_id": file_id,
        "owner": owner,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": int(payload.size_bytes or 0),
        "s3_key": s3_key,
        "status": STATUS_PENDING,
        "upload_time": now,
        "created_at": now,
    }
    repo.create(item)
    expires_in = presign_expiry_seconds()
    upload_url = store.presigned_put_url(s3_key, content_type, expires_in)
    logger.info("issued upload url file_id=%s owner=%s", file_id, owner)
    return UploadUrlResponse(
        file_id=file_id,
        s3_key=s3_key,
        upload_url=upload_url,
        expires_in_seconds=expires_in,
    )


@app.post("/files/{file_id}/confirm", response_model=FileMetadata)
def confirm_upload(
    file_id: str,
    repo: FileRepository = Depends(get_repository),
    store: ObjectStore = Depends(get_object_store),
) -> FileMetadata:
    """Verify the object exists in S3 and mark the metadata record available."""
    item = repo.get(file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="file not found")

    head = store.head_object(str(item.get("s3_key", "")))
    if head is None:
        raise HTTPException(status_code=409, detail="object has not been uploaded to S3 yet")

    last_modified = head.get("LastModified")
    if isinstance(last_modified, datetime):
        upload_time = last_modified.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        upload_time = _utc_now()

    updates: Dict[str, Any] = {
        "size_bytes": int(head.get("ContentLength") or 0),
        "upload_time": upload_time,
        "status": STATUS_AVAILABLE,
    }
    content_type = head.get("ContentType")
    if content_type:
        updates["content_type"] = str(content_type)

    updated = repo.update(file_id, updates) or {}
    merged = dict(item)
    merged.update(updates)
    merged.update(updated)
    logger.info("confirmed upload file_id=%s size=%s", file_id, updates["size_bytes"])
    return _to_model(merged)


@app.get("/files", response_model=FileListResponse)
def list_files(
    owner: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    next_token: Optional[str] = Query(default=None),
    repo: FileRepository = Depends(get_repository),
) -> FileListResponse:
    """List the files belonging to an owner, newest first."""
    start_key = None
    if next_token:
        try:
            start_key = decode_token(next_token)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid next_token")

    items, last_key = repo.list_by_owner(owner, limit=limit, start_key=start_key)
    return FileListResponse(
        owner=owner,
        items=[_to_model(item) for item in items],
        count=len(items),
        next_token=encode_token(last_key) if last_key else None,
    )


@app.get("/files/{file_id}", response_model=FileMetadata)
def get_file(
    file_id: str,
    repo: FileRepository = Depends(get_repository),
) -> FileMetadata:
    """Return the metadata record for a single file."""
    item = repo.get(file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="file not found")
    return _to_model(item)


@app.get("/files/{file_id}/download-url", response_model=DownloadUrlResponse)
def create_download_url(
    file_id: str,
    repo: FileRepository = Depends(get_repository),
    store: ObjectStore = Depends(get_object_store),
) -> DownloadUrlResponse:
    """Return a presigned S3 GET URL for the file."""
    item = repo.get(file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="file not found")
    if str(item.get("status")) != STATUS_AVAILABLE:
        raise HTTPException(status_code=409, detail="file upload has not been confirmed")

    expires_in = presign_expiry_seconds()
    url = store.presigned_get_url(
        str(item.get("s3_key", "")),
        expires_in,
        filename=str(item.get("filename") or ""),
    )
    return DownloadUrlResponse(file_id=file_id, download_url=url, expires_in_seconds=expires_in)


@app.delete("/files/{file_id}", response_model=DeleteResponse)
def delete_file(
    file_id: str,
    repo: FileRepository = Depends(get_repository),
    store: ObjectStore = Depends(get_object_store),
) -> DeleteResponse:
    """Delete the S3 object and its metadata record."""
    item = repo.get(file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="file not found")

    store.delete_object(str(item.get("s3_key", "")))
    repo.delete(file_id)
    logger.info("deleted file_id=%s owner=%s", file_id, item.get("owner"))
    return DeleteResponse(file_id=file_id, deleted=True)


@app.get("/usage", response_model=UsageResponse)
def storage_usage(
    owner: Optional[str] = Query(default=None, max_length=128),
    repo: FileRepository = Depends(get_repository),
) -> UsageResponse:
    """Return per-owner storage usage, optionally filtered to one owner."""
    if owner:
        owners = [_usage_for(owner, repo.all_by_owner(owner))]
    else:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in repo.scan_all():
            grouped.setdefault(str(item.get("owner", "")), []).append(item)
        owners = [_usage_for(name, rows) for name, rows in sorted(grouped.items())]

    total = sum(entry.total_bytes for entry in owners)
    return UsageResponse(owners=owners, total_bytes=total)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
