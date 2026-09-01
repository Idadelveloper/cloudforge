"""FastAPI application exposing the image gallery HTTP API.

Routes are thin: all AWS interaction lives in :mod:`storage` behind the
``DynamoS3GalleryRepository`` interface, which is injected as a FastAPI
dependency so tests can substitute an in-memory fake.
"""

import logging
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import (
    AlbumNotFound,
    DynamoS3GalleryRepository,
    ImageNotFound,
    ObjectNotUploaded,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("image_gallery.app")

app = FastAPI(
    title="Image Gallery Backend",
    version="1.0.0",
    description="Albums in DynamoDB, image binaries in S3 uploaded through presigned URLs.",
)


@lru_cache(maxsize=1)
def get_repository() -> DynamoS3GalleryRepository:
    """Return the process-wide repository instance (overridden in tests)."""
    return DynamoS3GalleryRepository()


class AlbumCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class AlbumUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)


class AlbumResponse(BaseModel):
    album_id: str
    title: str
    description: str = ""
    image_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class AlbumListResponse(BaseModel):
    albums: List[AlbumResponse] = []
    next_token: Optional[str] = None


class ImageCreate(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=255)


class ImageResponse(BaseModel):
    album_id: str
    image_id: str
    filename: str = ""
    s3_key: str = ""
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    status: str = "pending"
    created_at: str = ""
    uploaded_at: Optional[str] = None
    download_url: Optional[str] = None


class ImageListResponse(BaseModel):
    album_id: str
    count: int = 0
    images: List[ImageResponse] = []


class PresignedUploadResponse(BaseModel):
    album_id: str
    image_id: str
    filename: str
    s3_key: str
    status: str = "pending"
    upload_url: str
    expires_in: int
    required_headers: Dict[str, str] = {}


def _dump(model: BaseModel, exclude_unset: bool = False) -> Dict[str, Any]:
    """Dump a pydantic model to a dict (works with pydantic v1 and v2)."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=exclude_unset)
    return model.dict(exclude_unset=exclude_unset)  # pragma: no cover - pydantic v1 fallback


def _validate_filename(filename: str) -> str:
    cleaned = filename.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename")
    return cleaned


@app.exception_handler(AlbumNotFound)
async def _album_not_found_handler(request: Request, exc: AlbumNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Album '{0}' not found".format(exc)})


@app.exception_handler(ImageNotFound)
async def _image_not_found_handler(request: Request, exc: ImageNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Image '{0}' not found".format(exc)})


@app.exception_handler(ObjectNotUploaded)
async def _object_not_uploaded_handler(request: Request, exc: ObjectNotUploaded) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": "No object uploaded yet for key '{0}'".format(exc)},
    )


@app.get("/health")
def health(repo: Any = Depends(get_repository)) -> JSONResponse:
    """Report whether the S3 bucket and both DynamoDB tables are reachable."""
    result = repo.health()
    healthy = bool(result.get("ok"))
    payload = {
        "status": "ok" if healthy else "degraded",
        "checks": result.get("checks", {}),
    }
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content=payload)


@app.post("/albums", response_model=AlbumResponse, status_code=status.HTTP_201_CREATED)
def create_album(payload: AlbumCreate, repo: Any = Depends(get_repository)) -> Dict[str, Any]:
    """Create a new album metadata record."""
    return repo.create_album(payload.title, payload.description or "")


@app.get("/albums", response_model=AlbumListResponse)
def list_albums(
    limit: int = Query(default=50, ge=1, le=100),
    next_token: Optional[str] = Query(default=None),
    repo: Any = Depends(get_repository),
) -> Dict[str, Any]:
    """List albums (paginated)."""
    return repo.list_albums(limit=limit, next_token=next_token)


@app.get("/albums/{album_id}", response_model=AlbumResponse)
def get_album(album_id: str, repo: Any = Depends(get_repository)) -> Dict[str, Any]:
    """Fetch a single album."""
    album = repo.get_album(album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
    return album


@app.patch("/albums/{album_id}", response_model=AlbumResponse)
def update_album(album_id: str, payload: AlbumUpdate, repo: Any = Depends(get_repository)) -> Dict[str, Any]:
    """Update mutable album metadata."""
    updates = {key: value for key, value in _dump(payload, exclude_unset=True).items() if value is not None}
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No updatable fields supplied")
    return repo.update_album(album_id, updates)


@app.delete("/albums/{album_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_album(album_id: str, repo: Any = Depends(get_repository)) -> Response:
    """Delete an album, its image metadata and its S3 objects."""
    repo.delete_album(album_id)
    LOGGER.info("deleted album %s", album_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/albums/{album_id}/images",
    response_model=PresignedUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_image(album_id: str, payload: ImageCreate, repo: Any = Depends(get_repository)) -> Dict[str, Any]:
    """Register a pending image and hand back a presigned S3 PUT URL."""
    filename = _validate_filename(payload.filename)
    result = repo.create_image(album_id, filename, payload.content_type)
    image = result["image"]
    return {
        "album_id": image["album_id"],
        "image_id": image["image_id"],
        "filename": image["filename"],
        "s3_key": image["s3_key"],
        "status": image["status"],
        "upload_url": result["upload_url"],
        "expires_in": result["expires_in"],
        "required_headers": {"Content-Type": image["content_type"]},
    }


@app.post("/albums/{album_id}/images/{image_id}/complete", response_model=ImageResponse)
def complete_image(album_id: str, image_id: str, repo: Any = Depends(get_repository)) -> Dict[str, Any]:
    """Confirm an upload finished and flip the image to ``available``."""
    return repo.complete_image(album_id, image_id)


@app.get("/albums/{album_id}/images", response_model=ImageListResponse)
def list_images(album_id: str, repo: Any = Depends(get_repository)) -> Dict[str, Any]:
    """List image metadata for an album."""
    images = repo.list_images(album_id)
    return {"album_id": album_id, "count": len(images), "images": images}


@app.get("/albums/{album_id}/images/{image_id}", response_model=ImageResponse)
def get_image(album_id: str, image_id: str, repo: Any = Depends(get_repository)) -> Dict[str, Any]:
    """Fetch a single image's metadata plus a presigned download URL."""
    return repo.get_image(album_id, image_id)


@app.delete("/albums/{album_id}/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_image(album_id: str, image_id: str, repo: Any = Depends(get_repository)) -> Response:
    """Delete one image (S3 object + metadata item)."""
    repo.delete_image(album_id, image_id)
    LOGGER.info("deleted image %s from album %s", image_id, album_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def main() -> None:  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    main()
