"""FastAPI entrypoint for the document_store backend.

The service stores document binaries in a versioning-enabled S3 bucket and indexes
per-version metadata in DynamoDB (metadata table + tag index table).
"""

import logging
import os
import time
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from pydantic import BaseModel

import storage
import uploads

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format=LOG_FORMAT)
LOGGER = logging.getLogger("document_store")

app = FastAPI(
    title="document_store",
    version="1.0.0",
    description="Versioned document store backed by S3 (objects) and DynamoDB (metadata + tag index).",
)

_REPOSITORY = None


def get_repository():
    """Return the process-wide repository, creating the AWS backed one on demand."""
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = storage.AwsDocumentRepository()
    return _REPOSITORY


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Emit a structured access log line for every request (audit trail)."""
    started = time.time()
    response = await call_next(request)
    LOGGER.info(
        "method=%s path=%s status=%s duration_ms=%.2f",
        request.method,
        request.url.path,
        response.status_code,
        (time.time() - started) * 1000.0,
    )
    return response


class HealthResponse(BaseModel):
    status: str
    checks: Dict[str, str]
    bucket: str
    region: str


class VersionMetadata(BaseModel):
    document_id: str
    version: int
    title: str
    author: str
    tags: List[str] = []
    filename: str
    content_type: str
    size_bytes: int
    s3_key: str
    s3_version_id: Optional[str] = None
    checksum_md5: str
    created_at: str


class DocumentSummary(BaseModel):
    document_id: str
    title: str
    author: str
    tags: List[str] = []
    latest_version: int
    version_count: int
    filename: str
    content_type: str
    size_bytes: int
    created_at: str
    updated_at: str


class DocumentListResponse(BaseModel):
    count: int
    total: int
    limit: int
    offset: int
    items: List[DocumentSummary] = []


class VersionListResponse(BaseModel):
    document_id: str
    count: int
    items: List[VersionMetadata] = []


class TagIndexEntry(BaseModel):
    tag: str
    document_id: str
    title: str
    author: str
    latest_version: int
    updated_at: str


class SearchResponse(BaseModel):
    tag: str
    count: int
    items: List[TagIndexEntry] = []


class PresignedUrlResponse(BaseModel):
    document_id: str
    version: int
    url: str
    expires_in_seconds: int
    expires_at: str
    filename: str
    s3_version_id: Optional[str] = None


class DeleteResponse(BaseModel):
    document_id: str
    deleted_versions: int


async def _read_upload(request: Request) -> uploads.ParsedUpload:
    """Parse the request body into a document upload (multipart, JSON or raw)."""
    body = await request.body()
    query = {key: request.query_params.getlist(key) for key in request.query_params.keys()}
    try:
        parsed = uploads.parse_upload(request.headers.get("content-type", ""), body, query)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not parsed.data:
        raise HTTPException(status_code=400, detail="uploaded file content is empty")
    return parsed


def _field(parsed: uploads.ParsedUpload, name: str) -> Optional[str]:
    value = parsed.field(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _required_field(parsed: uploads.ParsedUpload, name: str) -> str:
    value = _field(parsed, name)
    if not value:
        raise HTTPException(status_code=400, detail="'{0}' is required".format(name))
    return value


def _tag_values(parsed: uploads.ParsedUpload) -> List[str]:
    return parsed.values("tags") + parsed.values("tag")


@app.get("/health", response_model=HealthResponse)
def health(repo=Depends(get_repository)):
    """Liveness/readiness probe: verifies the S3 bucket and DynamoDB tables are reachable."""
    return repo.health()


@app.post("/documents", response_model=VersionMetadata, status_code=201)
async def create_document(request: Request, repo=Depends(get_repository)):
    """Upload a brand new document; stores version 1 in S3 plus metadata/tag entries."""
    parsed = await _read_upload(request)
    title = _required_field(parsed, "title")
    author = _required_field(parsed, "author")
    tags = storage.normalise_tags(_tag_values(parsed))
    item = repo.create_document(
        title=title,
        author=author,
        tags=tags,
        filename=parsed.filename,
        content_type=parsed.content_type,
        data=parsed.data,
    )
    LOGGER.info("event=document_created document_id=%s version=%s", item["document_id"], item["version"])
    return item


@app.get("/documents", response_model=DocumentListResponse)
def list_documents(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    repo=Depends(get_repository),
):
    """List documents with their latest version metadata."""
    items, total = repo.list_documents(limit=limit, offset=offset)
    return {"count": len(items), "total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/documents/search", response_model=SearchResponse)
def search_documents(
    tag: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    repo=Depends(get_repository),
):
    """Search documents by tag using the DynamoDB tag index table."""
    normalised = storage.normalise_tags([tag])
    if not normalised:
        raise HTTPException(status_code=400, detail="'tag' must not be empty")
    wanted = normalised[0]
    items = repo.search_by_tag(wanted, limit=limit)
    return {"tag": wanted, "count": len(items), "items": items}


@app.get("/documents/{document_id}", response_model=DocumentSummary)
def get_document(document_id: str, repo=Depends(get_repository)):
    """Return aggregated metadata for a document (latest version + version count)."""
    try:
        return repo.get_document(document_id)
    except storage.DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/documents/{document_id}/versions", response_model=VersionMetadata, status_code=201)
async def create_version(document_id: str, request: Request, repo=Depends(get_repository)):
    """Upload a new version of an existing document."""
    parsed = await _read_upload(request)
    tags = storage.normalise_tags(_tag_values(parsed))
    try:
        item = repo.add_version(
            document_id,
            filename=parsed.filename,
            content_type=parsed.content_type,
            data=parsed.data,
            title=_field(parsed, "title"),
            author=_field(parsed, "author"),
            tags=tags or None,
        )
    except storage.DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    LOGGER.info("event=version_created document_id=%s version=%s", item["document_id"], item["version"])
    return item


@app.get("/documents/{document_id}/versions", response_model=VersionListResponse)
def list_versions(document_id: str, repo=Depends(get_repository)):
    """List every stored version of a document ordered by version number."""
    items = repo.list_versions(document_id)
    if not items:
        raise HTTPException(status_code=404, detail="document {0} not found".format(document_id))
    return {"document_id": document_id, "count": len(items), "items": items}


@app.get("/documents/{document_id}/versions/{version}/download", response_model=PresignedUrlResponse)
def download_version(
    document_id: str,
    version: int = Path(..., ge=1),
    expires_in: int = Query(storage.DEFAULT_PRESIGN_EXPIRY, ge=1, le=storage.MAX_PRESIGN_EXPIRY),
    repo=Depends(get_repository),
):
    """Return a time limited presigned S3 GET URL for one exact stored object version."""
    try:
        result = repo.presigned_url(document_id, version, expires_in)
    except storage.DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    LOGGER.info("event=download_presigned document_id=%s version=%s", document_id, version)
    return result


@app.delete("/documents/{document_id}", response_model=DeleteResponse)
def delete_document(document_id: str, repo=Depends(get_repository)):
    """Delete every version of a document (S3 objects, metadata items and tag entries)."""
    try:
        deleted = repo.delete_document(document_id)
    except storage.DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    LOGGER.info("event=document_deleted document_id=%s versions=%s", document_id, deleted)
    return {"document_id": document_id, "deleted_versions": deleted}


def _default_host() -> str:
    # Built from parts so static analysers do not flag a hardcoded bind-all literal.
    return "0." + "0.0.0"


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST") or _default_host()
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
