"""FastAPI application entrypoint for the file sharing backend."""

import logging
import os
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import (
    DynamoS3FileStore,
    FileStore,
    InvalidTokenError,
    MAX_PAGE_SIZE,
    NotFoundError,
    StorageError,
    aws_region,
    bucket_name,
    owner_index_name,
    presign_expires_in,
    table_name,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("file_sharing_backend")

app = FastAPI(
    title="file_sharing_backend",
    version="1.0.0",
    description="Share files through S3 presigned URLs with metadata tracked in DynamoDB.",
)

_STORE: Optional[FileStore] = None


def get_store() -> FileStore:
    """Return the process-wide storage adapter (lazily constructed)."""
    global _STORE
    if _STORE is None:
        _STORE = DynamoS3FileStore()
    return _STORE


class HealthResponse(BaseModel):
    status: str
    bucket: str
    table: str
    owner_index: str
    region: str
    endpoint_url: Optional[str] = None


class UploadUrlRequest(BaseModel):
    owner: str = Field(..., min_length=1, max_length=128)
    filename: str = Field(..., min_length=1, max_length=512)
    content_type: str = Field(default="application/octet-stream", min_length=1, max_length=255)
    size_bytes: Optional[int] = Field(default=None, ge=0)


class UploadUrlResponse(BaseModel):
    file_id: str
    upload_url: str
    s3_key: str
    expires_in: int


class FileMetadata(BaseModel):
    file_id: str
    owner: str
    filename: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    s3_key: str
    status: str = "pending"
    uploaded_at: Optional[str] = None
    download_url: Optional[str] = None


class FileListResponse(BaseModel):
    owner: str
    files: List[FileMetadata]
    count: int
    next_token: Optional[str] = None


class DeleteResponse(BaseModel):
    file_id: str
    s3_key: str
    deleted: bool


class OwnerUsage(BaseModel):
    owner: str
    file_count: int
    total_bytes: int


class UsageResponse(BaseModel):
    owners: List[OwnerUsage]
    total_bytes: int


def _to_metadata(item: Dict, download_url: Optional[str] = None) -> FileMetadata:
    """Build a response model from a raw metadata record."""
    return FileMetadata(
        file_id=str(item.get("file_id", "")),
        owner=str(item.get("owner", "")),
        filename=str(item.get("filename", "")),
        content_type=str(item.get("content_type", "application/octet-stream")),
        size_bytes=int(item.get("size_bytes", 0) or 0),
        s3_key=str(item.get("s3_key", "")),
        status=str(item.get("status", "pending")),
        uploaded_at=item.get("uploaded_at"),
        download_url=download_url,
    )


@app.exception_handler(InvalidTokenError)
async def invalid_token_handler(request: Request, exc: InvalidTokenError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(StorageError)
async def storage_error_handler(request: Request, exc: StorageError) -> JSONResponse:
    LOGGER.error("storage failure: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness probe describing the configured AWS resources."""
    return HealthResponse(
        status="ok",
        bucket=bucket_name(),
        table=table_name(),
        owner_index=owner_index_name(),
        region=aws_region(),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


@app.post("/files/upload-url", response_model=UploadUrlResponse, status_code=201)
def create_upload_url(
    payload: UploadUrlRequest,
    store: FileStore = Depends(get_store),
) -> UploadUrlResponse:
    """Issue a presigned S3 PUT URL and record a pending metadata item."""
    owner = payload.owner.strip()
    filename = payload.filename.strip()
    if not owner or not filename:
        raise HTTPException(status_code=422, detail="owner and filename must not be blank")
    result = store.create_upload_url(
        owner=owner,
        filename=filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
    )
    LOGGER.info("issued upload url owner=%s file_id=%s", owner, result.get("file_id"))
    return UploadUrlResponse(
        file_id=str(result["file_id"]),
        upload_url=str(result["upload_url"]),
        s3_key=str(result["s3_key"]),
        expires_in=int(result["expires_in"]),
    )


@app.post("/files/{file_id}/complete", response_model=FileMetadata)
def complete_upload(file_id: str, store: FileStore = Depends(get_store)) -> FileMetadata:
    """Confirm an upload and refresh the metadata record from S3."""
    item = store.complete_upload(file_id)
    LOGGER.info("upload completed file_id=%s size=%s", file_id, item.get("size_bytes"))
    return _to_metadata(item)


@app.get("/files", response_model=FileListResponse)
def list_files(
    owner: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(default=25, ge=1, le=MAX_PAGE_SIZE),
    next_token: Optional[str] = Query(default=None, max_length=4096),
    store: FileStore = Depends(get_store),
) -> FileListResponse:
    """List metadata for the files owned by ``owner``."""
    items, token = store.list_files(owner=owner, limit=limit, next_token=next_token)
    files = [_to_metadata(item) for item in items]
    return FileListResponse(owner=owner, files=files, count=len(files), next_token=token)


@app.get("/files/{file_id}", response_model=FileMetadata)
def get_file(file_id: str, store: FileStore = Depends(get_store)) -> FileMetadata:
    """Fetch a single metadata record together with a presigned download URL."""
    item = store.get_file(file_id)
    url = store.download_url(str(item.get("s3_key", "")))
    return _to_metadata(item, download_url=url)


@app.delete("/files/{file_id}", response_model=DeleteResponse)
def delete_file(file_id: str, store: FileStore = Depends(get_store)) -> DeleteResponse:
    """Hard delete the S3 object and its metadata record."""
    item = store.delete_file(file_id)
    LOGGER.info("deleted file_id=%s key=%s", file_id, item.get("s3_key"))
    return DeleteResponse(file_id=file_id, s3_key=str(item.get("s3_key", "")), deleted=True)


@app.get("/usage", response_model=UsageResponse)
def usage(
    owner: Optional[str] = Query(default=None, max_length=128),
    store: FileStore = Depends(get_store),
) -> UsageResponse:
    """Report storage usage for one owner, or for every owner when omitted."""
    normalized = owner.strip() if owner else None
    owners, total = store.usage(owner=normalized or None)
    return UsageResponse(
        owners=[OwnerUsage(**row) for row in owners],
        total_bytes=total,
    )


def main() -> None:  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    LOGGER.info("starting service with presign expiry %ss", presign_expires_in())
    main()
