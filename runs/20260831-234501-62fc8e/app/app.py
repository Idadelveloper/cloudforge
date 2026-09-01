"""FastAPI application exposing the image gallery API.

Album and image metadata live in DynamoDB, image bytes live in S3 and are
transferred directly by the client using presigned URLs issued here.
"""

import logging
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import (
    BadRequestError,
    ConflictError,
    GalleryRepository,
    NotFoundError,
    StorageError,
    load_settings,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("image_gallery.app")


class AlbumCreateRequest(BaseModel):
    """Payload used to create a new album."""

    title: str = Field(min_length=1, max_length=256)
    description: Optional[str] = Field(default=None, max_length=2048)


class AlbumUpdateRequest(BaseModel):
    """Payload used to patch an album."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=256)
    description: Optional[str] = Field(default=None, max_length=2048)


class AlbumResponse(BaseModel):
    """Album metadata returned to clients."""

    album_id: str
    title: str
    description: Optional[str] = None
    image_count: int = 0
    created_at: str
    updated_at: str


class AlbumListResponse(BaseModel):
    """Paginated list of albums."""

    items: List[AlbumResponse]
    count: int
    next_token: Optional[str] = None


class AlbumDeleteResponse(BaseModel):
    """Result of a cascading album delete."""

    album_id: str
    deleted: bool = True
    deleted_images: int = 0
    deleted_objects: int = 0


class PresignedUploadRequest(BaseModel):
    """Payload used to request a presigned upload URL."""

    filename: str = Field(min_length=1, max_length=512)
    content_type: str = Field(default="application/octet-stream", min_length=1, max_length=255)
    size_bytes: Optional[int] = Field(default=None, ge=0)


class PresignedUploadResponse(BaseModel):
    """Presigned upload instructions handed back to the client."""

    image_id: str
    upload_url: str
    method: str = "PUT"
    s3_key: str
    expires_in: int
    content_type: str


class ImageResponse(BaseModel):
    """Image metadata, optionally with a presigned download URL."""

    album_id: str
    image_id: str
    filename: str
    content_type: str
    size_bytes: Optional[int] = None
    etag: Optional[str] = None
    status: str
    created_at: str
    uploaded_at: Optional[str] = None
    download_url: Optional[str] = None


class ImageListResponse(BaseModel):
    """Paginated list of images inside an album."""

    album_id: str
    items: List[ImageResponse]
    count: int
    next_token: Optional[str] = None


class ImageDeleteResponse(BaseModel):
    """Result of deleting a single image."""

    album_id: str
    image_id: str
    deleted: bool = True


class ErrorResponse(BaseModel):
    """Uniform error body."""

    error: str
    detail: Optional[str] = None


@lru_cache(maxsize=1)
def get_repository() -> GalleryRepository:
    """Build (once) the repository used by the request handlers."""
    return GalleryRepository(load_settings())


app = FastAPI(
    title="image_gallery_backend",
    version="1.0.0",
    description="Albums in DynamoDB, images in S3 via presigned URLs.",
)

_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(NotFoundError)
async def _not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.exception_handler(ConflictError)
async def _conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(BadRequestError)
async def _bad_request_handler(request: Request, exc: BadRequestError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "bad_request", "detail": str(exc)})


@app.exception_handler(StorageError)
async def _storage_error_handler(request: Request, exc: StorageError) -> JSONResponse:
    LOGGER.error("storage failure: %s", exc)
    return JSONResponse(status_code=502, content={"error": "storage_error", "detail": str(exc)})


@app.get("/health")
def health(repo: GalleryRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Report service status plus reachability of S3 and DynamoDB."""
    return repo.health()


@app.post("/albums", response_model=AlbumResponse, status_code=status.HTTP_201_CREATED)
def create_album(
    payload: AlbumCreateRequest,
    repo: GalleryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Create a new album."""
    return repo.create_album(payload.title, payload.description)


@app.get("/albums", response_model=AlbumListResponse)
def list_albums(
    limit: int = Query(default=50, ge=1, le=200),
    next_token: Optional[str] = Query(default=None),
    repo: GalleryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List albums with cursor pagination."""
    items, token = repo.list_albums(limit=limit, next_token=next_token)
    return {"items": items, "count": len(items), "next_token": token}


@app.get("/albums/{album_id}", response_model=AlbumResponse)
def get_album(album_id: str, repo: GalleryRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Fetch a single album."""
    return repo.get_album(album_id)


@app.patch("/albums/{album_id}", response_model=AlbumResponse)
def update_album(
    album_id: str,
    payload: AlbumUpdateRequest,
    repo: GalleryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Update an album title and/or description."""
    if payload.title is None and payload.description is None:
        raise BadRequestError("at least one of 'title' or 'description' must be provided")
    return repo.update_album(album_id, title=payload.title, description=payload.description)


@app.delete("/albums/{album_id}", response_model=AlbumDeleteResponse)
def delete_album(album_id: str, repo: GalleryRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Delete an album and every image (S3 object + metadata) it contains."""
    return repo.delete_album(album_id)


@app.post(
    "/albums/{album_id}/images",
    response_model=PresignedUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_image_upload(
    album_id: str,
    payload: PresignedUploadRequest,
    repo: GalleryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Register a pending image and return a presigned PUT URL."""
    return repo.create_pending_image(
        album_id,
        filename=payload.filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
    )


@app.post("/albums/{album_id}/images/{image_id}/complete", response_model=ImageResponse)
def complete_image_upload(
    album_id: str,
    image_id: str,
    repo: GalleryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Confirm that the client finished uploading the image bytes to S3."""
    return repo.complete_image(album_id, image_id)


@app.get("/albums/{album_id}/images", response_model=ImageListResponse)
def list_images(
    album_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    next_token: Optional[str] = Query(default=None),
    repo: GalleryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List the images of an album, each with a presigned download URL."""
    items, token = repo.list_images(album_id, limit=limit, next_token=next_token)
    return {"album_id": album_id, "items": items, "count": len(items), "next_token": token}


@app.get("/albums/{album_id}/images/{image_id}", response_model=ImageResponse)
def get_image(
    album_id: str,
    image_id: str,
    repo: GalleryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Fetch one image's metadata plus a fresh presigned download URL."""
    return repo.get_image(album_id, image_id)


@app.delete("/albums/{album_id}/images/{image_id}", response_model=ImageDeleteResponse)
def delete_image(
    album_id: str,
    image_id: str,
    repo: GalleryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Delete a single image (S3 object + metadata) and update the counter."""
    return repo.delete_image(album_id, image_id)


def main() -> None:
    """Run the service with uvicorn."""
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
