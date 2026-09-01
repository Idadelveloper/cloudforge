"""FastAPI entrypoint for the document_store service.

Routes accept multipart/form-data (parsed with a small stdlib parser) or a JSON
envelope carrying base64 content, so the service has no non-stdlib parsing
dependencies. All AWS access happens through the repository in ``storage.py``,
which can be swapped out via FastAPI dependency overrides in tests.
"""

import base64
import binascii
import hmac
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel

import storage
import uploads

APP_NAME = "document_store"
LOGGER = logging.getLogger(APP_NAME)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(
    title="Document Store",
    version="1.0.0",
    description="Versioned document storage backed by S3 (binaries) and DynamoDB (metadata index).",
)

_repository: Optional[storage.DocumentRepository] = None


def get_repository() -> storage.DocumentRepository:
    """Return the process-wide repository, building the AWS one on first use."""
    global _repository
    if _repository is None:
        _repository = storage.AwsDocumentRepository()
    return _repository


def set_repository(repository: Optional[storage.DocumentRepository]) -> None:
    """Replace the process-wide repository (used by local runners and tests)."""
    global _repository
    _repository = repository


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Validate the static API key on write endpoints when one is configured."""
    expected = storage.get_settings().api_key
    if not expected:
        return
    if not x_api_key or not hmac.compare_digest(str(x_api_key), str(expected)):
        raise HTTPException(status_code=401, detail="missing or invalid API key")


class HealthResponse(BaseModel):
    status: str
    service: str
    dependencies: Dict[str, str] = {}


class DocumentVersionModel(BaseModel):
    document_id: str
    version: int
    title: str
    author: str
    tags: List[str] = []
    s3_key: str
    s3_version_id: str
    content_type: str
    size_bytes: int
    checksum: Optional[str] = None
    created_at: str
    is_latest: bool = False
    filename: Optional[str] = None


class DocumentListResponse(BaseModel):
    count: int
    items: List[DocumentVersionModel] = []
    next_token: Optional[str] = None


class VersionListResponse(BaseModel):
    document_id: str
    count: int
    versions: List[DocumentVersionModel] = []


class SearchResponse(BaseModel):
    tag: str
    count: int
    items: List[DocumentVersionModel] = []


class PresignedUrlResponse(BaseModel):
    document_id: str
    version: int
    url: str
    expires_in_seconds: int
    expires_at: str


class DeleteResponse(BaseModel):
    document_id: str
    deleted_versions: int
    message: str


def _version_model(item: Dict[str, Any]) -> DocumentVersionModel:
    return DocumentVersionModel(
        document_id=str(item.get("document_id", "")),
        version=int(item.get("version", 0)),
        title=str(item.get("title", "")),
        author=str(item.get("author", "")),
        tags=[str(tag) for tag in (item.get("tags") or [])],
        s3_key=str(item.get("s3_key", "")),
        s3_version_id=str(item.get("s3_version_id", "null")),
        content_type=str(item.get("content_type", "application/octet-stream")),
        size_bytes=int(item.get("size_bytes", 0)),
        checksum=item.get("checksum"),
        created_at=str(item.get("created_at", "")),
        is_latest=bool(item.get("is_latest", False)),
        filename=item.get("filename"),
    )


def _decode_json_upload(body: bytes) -> Tuple[Dict[str, Any], uploads.UploadedFile]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    encoded = payload.get("content_base64") or payload.get("content") or ""
    try:
        content = base64.b64decode(str(encoded), validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="content_base64 is not valid base64")
    fields: Dict[str, Any] = {
        "title": payload.get("title"),
        "author": payload.get("author"),
        "tags": payload.get("tags"),
    }
    upload = uploads.UploadedFile(
        filename=str(payload.get("filename") or "document.bin"),
        content_type=str(payload.get("content_type") or "application/octet-stream"),
        content=content,
    )
    return fields, upload


async def _extract_upload(request: Request) -> Tuple[Dict[str, Any], uploads.UploadedFile]:
    """Pull form fields and the uploaded file out of a multipart or JSON request."""
    settings = storage.get_settings()
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    if len(body) > (settings.max_upload_bytes * 2) + 8192:
        raise HTTPException(status_code=413, detail="request body exceeds maximum upload size")

    lowered = content_type.lower()
    if lowered.startswith("multipart/form-data"):
        try:
            form = uploads.parse_multipart_form(body, content_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        upload = form.files.get("file")
        if upload is None:
            raise HTTPException(status_code=400, detail="missing 'file' part in multipart body")
        fields: Dict[str, Any] = dict(form.fields)
    elif lowered.startswith("application/json") or body.strip().startswith(b"{"):
        fields, upload = _decode_json_upload(body)
    else:
        raise HTTPException(
            status_code=415,
            detail="unsupported content type; use multipart/form-data or application/json",
        )

    if not upload.content:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(upload.content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="uploaded file exceeds maximum size")
    return fields, upload


def _required_field(fields: Dict[str, Any], name: str) -> str:
    value = fields.get(name)
    text = "" if value is None else str(value).strip()
    if not text:
        raise HTTPException(status_code=400, detail="field '%s' is required" % name)
    return text


def _optional_field(fields: Dict[str, Any], name: str) -> Optional[str]:
    value = fields.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@app.get("/health", response_model=HealthResponse)
def health(repo: storage.DocumentRepository = Depends(get_repository)) -> HealthResponse:
    result = repo.health()
    dependencies = {str(k): str(v) for k, v in (result.get("dependencies") or {}).items()}
    status = "ok" if result.get("healthy") else "degraded"
    return HealthResponse(status=status, service=APP_NAME, dependencies=dependencies)


@app.post("/documents", response_model=DocumentVersionModel, status_code=201)
async def create_document(
    request: Request,
    repo: storage.DocumentRepository = Depends(get_repository),
    _auth: None = Depends(require_api_key),
) -> DocumentVersionModel:
    fields, upload = await _extract_upload(request)
    title = _required_field(fields, "title")
    author = _required_field(fields, "author")
    item = repo.create_document(
        title=title,
        author=author,
        tags=storage.normalize_tags(fields.get("tags")),
        filename=upload.filename,
        content_type=upload.content_type,
        data=upload.content,
    )
    LOGGER.info("stored document %s version %s", item.get("document_id"), item.get("version"))
    return _version_model(item)


@app.get("/documents", response_model=DocumentListResponse)
def list_documents(
    author: Optional[str] = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    next_token: Optional[str] = Query(default=None),
    repo: storage.DocumentRepository = Depends(get_repository),
) -> DocumentListResponse:
    try:
        items, token = repo.list_documents(author=author, limit=limit, next_token=next_token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    models = [_version_model(item) for item in items]
    return DocumentListResponse(count=len(models), items=models, next_token=token)


@app.post("/documents/{document_id}/versions", response_model=DocumentVersionModel, status_code=201)
async def create_document_version(
    request: Request,
    document_id: str = Path(..., min_length=1),
    repo: storage.DocumentRepository = Depends(get_repository),
    _auth: None = Depends(require_api_key),
) -> DocumentVersionModel:
    fields, upload = await _extract_upload(request)
    item = repo.add_version(
        document_id=document_id,
        title=_optional_field(fields, "title"),
        author=_optional_field(fields, "author"),
        tags=storage.normalize_tags(fields.get("tags")) if fields.get("tags") is not None else None,
        filename=upload.filename,
        content_type=upload.content_type,
        data=upload.content,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="document not found")
    LOGGER.info("stored new version %s for document %s", item.get("version"), document_id)
    return _version_model(item)


@app.get("/documents/{document_id}/versions", response_model=VersionListResponse)
def list_document_versions(
    document_id: str = Path(..., min_length=1),
    repo: storage.DocumentRepository = Depends(get_repository),
) -> VersionListResponse:
    items = repo.list_versions(document_id)
    if not items:
        raise HTTPException(status_code=404, detail="document not found")
    models = [_version_model(item) for item in items]
    return VersionListResponse(document_id=document_id, count=len(models), versions=models)


@app.get("/documents/{document_id}/versions/{version}", response_model=DocumentVersionModel)
def get_document_version(
    document_id: str = Path(..., min_length=1),
    version: int = Path(..., ge=1),
    repo: storage.DocumentRepository = Depends(get_repository),
) -> DocumentVersionModel:
    item = repo.get_version(document_id, version)
    if item is None:
        raise HTTPException(status_code=404, detail="document version not found")
    return _version_model(item)


@app.get("/documents/{document_id}/versions/{version}/download-url", response_model=PresignedUrlResponse)
def get_download_url(
    document_id: str = Path(..., min_length=1),
    version: int = Path(..., ge=1),
    expires_in: Optional[int] = Query(default=None, ge=1, le=3600),
    repo: storage.DocumentRepository = Depends(get_repository),
) -> PresignedUrlResponse:
    settings = storage.get_settings()
    ttl = int(expires_in or settings.default_expiry)
    ttl = min(ttl, settings.max_expiry)
    payload = repo.create_presigned_url(document_id, version, ttl)
    if payload is None:
        raise HTTPException(status_code=404, detail="document version not found")
    return PresignedUrlResponse(
        document_id=str(payload.get("document_id", document_id)),
        version=int(payload.get("version", version)),
        url=str(payload.get("url", "")),
        expires_in_seconds=int(payload.get("expires_in_seconds", ttl)),
        expires_at=str(payload.get("expires_at", "")),
    )


@app.get("/search", response_model=SearchResponse)
def search_documents(
    tag: str = Query(..., min_length=1),
    limit: int = Query(default=25, ge=1, le=100),
    repo: storage.DocumentRepository = Depends(get_repository),
) -> SearchResponse:
    normalized = tag.strip().lower()
    if not normalized:
        raise HTTPException(status_code=400, detail="tag must not be blank")
    items = repo.search_by_tag(normalized, limit)
    models = [_version_model(item) for item in items]
    return SearchResponse(tag=normalized, count=len(models), items=models)


@app.delete("/documents/{document_id}", response_model=DeleteResponse)
def delete_document(
    document_id: str = Path(..., min_length=1),
    repo: storage.DocumentRepository = Depends(get_repository),
    _auth: None = Depends(require_api_key),
) -> DeleteResponse:
    deleted = repo.delete_document(document_id)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="document not found")
    LOGGER.info("deleted document %s (%s versions)", document_id, deleted)
    return DeleteResponse(
        document_id=document_id,
        deleted_versions=deleted,
        message="document and all versions deleted",
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
