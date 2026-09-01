"""FastAPI application for the blog platform backend.

Authors manage markdown posts (DynamoDB) and images (S3); readers submit
comments that are queued on SQS for moderation and only written to the
published comments table once a moderator approves them.
"""

import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import Storage, StorageError, build_storage, utc_now_iso
from uploads import parse_upload

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
LOGGER = logging.getLogger("blog_platform_backend")
AUDIT = logging.getLogger("blog_platform_backend.moderation")

ALLOWED_STATUSES = ("draft", "published")
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", "10485760"))

app = FastAPI(
    title="Blog Platform Backend",
    version="1.0.0",
    description="Markdown posts in DynamoDB, images in S3, comments moderated through SQS.",
)

_STORAGE: Optional[Storage] = None


def get_storage() -> Storage:
    """Return the process wide storage aggregate (overridable in tests)."""
    global _STORAGE
    if _STORAGE is None:
        _STORAGE = build_storage()
    return _STORAGE


@app.exception_handler(StorageError)
async def storage_error_handler(request: Request, exc: StorageError) -> JSONResponse:
    """Surface AWS failures as 502 responses instead of stack traces."""
    LOGGER.error("storage error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


class PostCreate(BaseModel):
    """Payload for creating a post."""

    title: str = Field(min_length=1, max_length=300)
    body_markdown: str = Field(default="", max_length=350000)
    author: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    status: str = Field(default="draft")


class PostUpdate(BaseModel):
    """Payload for updating a post; every field is optional."""

    title: Optional[str] = Field(default=None, max_length=300)
    body_markdown: Optional[str] = Field(default=None, max_length=350000)
    tags: Optional[List[str]] = None
    status: Optional[str] = None


class CommentSubmit(BaseModel):
    """Reader submitted comment awaiting moderation."""

    author_name: str = Field(min_length=1, max_length=120)
    author_email: Optional[str] = Field(default=None, max_length=254)
    body: str = Field(min_length=1, max_length=5000)


class PendingCommentPayload(BaseModel):
    """Comment payload as returned by the moderation poll endpoint."""

    comment_id: Optional[str] = None
    post_id: str = Field(min_length=1)
    author_name: str = Field(min_length=1, max_length=120)
    author_email: Optional[str] = None
    body: str = Field(min_length=1, max_length=5000)
    submitted_at: Optional[str] = None


class ApproveRequest(BaseModel):
    """Approve a pending comment and delete it from the queue."""

    receipt_handle: str = Field(min_length=1)
    comment: PendingCommentPayload
    approved_by: Optional[str] = None


class RejectRequest(BaseModel):
    """Reject a pending comment and delete it from the queue."""

    receipt_handle: str = Field(min_length=1)
    comment_id: Optional[str] = None
    reason: Optional[str] = Field(default=None, max_length=500)


def _dump(model: BaseModel) -> Dict[str, Any]:
    """Model dict that works on pydantic v1 and v2."""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _validate_status(value: str) -> str:
    if value not in ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="status must be one of {0}".format(list(ALLOWED_STATUSES)))
    return value


def _get_post_or_404(storage: Storage, post_id: str) -> Dict[str, Any]:
    post = storage.posts.get(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="post not found")
    return post


@app.get("/health")
def health(storage: Storage = Depends(get_storage)) -> Dict[str, Any]:
    """Liveness/readiness probe including AWS dependency reachability."""
    return storage.health()


@app.post("/posts", status_code=201)
def create_post(
    payload: PostCreate,
    x_author: Optional[str] = Header(default=None),
    storage: Storage = Depends(get_storage),
) -> Dict[str, Any]:
    """Create a markdown post."""
    now = utc_now_iso()
    item = {
        "post_id": uuid.uuid4().hex,
        "title": payload.title,
        "body_markdown": payload.body_markdown,
        "author": payload.author or x_author or "anonymous",
        "tags": list(payload.tags),
        "status": _validate_status(payload.status),
        "image_keys": [],
        "created_at": now,
        "updated_at": now,
    }
    created = storage.posts.create(item)
    LOGGER.info("created post %s", item["post_id"])
    return created or item


@app.get("/posts")
def list_posts(
    limit: int = Query(default=20, ge=1, le=100),
    next_token: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    storage: Storage = Depends(get_storage),
) -> Dict[str, Any]:
    """List posts with pagination and an optional status filter."""
    if status_filter is not None:
        _validate_status(status_filter)
    try:
        items, token = storage.posts.list_posts(limit=limit, next_token=next_token, status=status_filter)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"items": items, "count": len(items), "next_token": token}


@app.get("/posts/{post_id}")
def read_post(post_id: str, storage: Storage = Depends(get_storage)) -> Dict[str, Any]:
    """Read a single post."""
    return _get_post_or_404(storage, post_id)


@app.put("/posts/{post_id}")
def update_post(
    post_id: str,
    payload: PostUpdate,
    storage: Storage = Depends(get_storage),
) -> Dict[str, Any]:
    """Update mutable attributes of a post."""
    changes = {key: value for key, value in _dump(payload).items() if value is not None}
    if not changes:
        raise HTTPException(status_code=400, detail="no updatable fields supplied")
    if "status" in changes:
        _validate_status(changes["status"])
    changes["updated_at"] = utc_now_iso()
    updated = storage.posts.update(post_id, changes)
    if updated is None:
        raise HTTPException(status_code=404, detail="post not found")
    LOGGER.info("updated post %s", post_id)
    return updated


@app.delete("/posts/{post_id}", status_code=204)
def delete_post(post_id: str, storage: Storage = Depends(get_storage)) -> Response:
    """Delete a post item (images and comments are not cascaded)."""
    if not storage.posts.delete(post_id):
        raise HTTPException(status_code=404, detail="post not found")
    LOGGER.info("deleted post %s", post_id)
    return Response(status_code=204)


@app.post("/posts/{post_id}/images", status_code=201)
async def upload_image(
    post_id: str,
    request: Request,
    filename: Optional[str] = Query(default=None),
    storage: Storage = Depends(get_storage),
) -> Dict[str, Any]:
    """Upload an image for a post (multipart/form-data or a raw binary body)."""
    _get_post_or_404(storage, post_id)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty request body")
    if len(body) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image exceeds maximum allowed size")
    header_name = filename or request.headers.get("x-filename")
    try:
        upload = parse_upload(request.headers.get("content-type", ""), body, fallback_filename=header_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not upload.data:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    image_key = "{0}/{1}-{2}".format(post_id, uuid.uuid4().hex, upload.filename)
    storage.images.put_image(image_key, upload.data, upload.content_type)
    storage.posts.add_image_key(post_id, image_key)
    LOGGER.info("stored image %s for post %s", image_key, post_id)
    return {
        "post_id": post_id,
        "image_key": image_key,
        "content_type": upload.content_type,
        "size_bytes": len(upload.data),
        "uploaded_at": utc_now_iso(),
        "presigned_url": storage.images.presigned_url(image_key),
    }


@app.get("/posts/{post_id}/images")
def list_images(post_id: str, storage: Storage = Depends(get_storage)) -> Dict[str, Any]:
    """List a post's images with time limited presigned GET URLs."""
    post = _get_post_or_404(storage, post_id)
    keys = list(storage.images.list_keys("{0}/".format(post_id)))
    for recorded in post.get("image_keys") or []:
        if recorded not in keys:
            keys.append(recorded)
    images = [
        {"post_id": post_id, "image_key": key, "presigned_url": storage.images.presigned_url(key)}
        for key in keys
    ]
    return {"post_id": post_id, "count": len(images), "images": images}


@app.post("/posts/{post_id}/comments", status_code=202)
def submit_comment(
    post_id: str,
    payload: CommentSubmit,
    storage: Storage = Depends(get_storage),
) -> Dict[str, Any]:
    """Queue a reader comment for moderation."""
    _get_post_or_404(storage, post_id)
    comment = {
        "comment_id": uuid.uuid4().hex,
        "post_id": post_id,
        "author_name": payload.author_name,
        "author_email": payload.author_email,
        "body": payload.body,
        "submitted_at": utc_now_iso(),
    }
    message_id = storage.moderation.send_comment(comment)
    AUDIT.info("comment %s for post %s queued for moderation", comment["comment_id"], post_id)
    return {
        "comment_id": comment["comment_id"],
        "post_id": post_id,
        "status": "pending_moderation",
        "message_id": message_id,
        "submitted_at": comment["submitted_at"],
    }


@app.get("/posts/{post_id}/comments")
def list_comments(
    post_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    storage: Storage = Depends(get_storage),
) -> Dict[str, Any]:
    """List approved comments for a post."""
    _get_post_or_404(storage, post_id)
    comments = storage.comments.list_for_post(post_id, limit=limit)
    return {"post_id": post_id, "count": len(comments), "comments": comments}


@app.get("/moderation/comments")
def poll_moderation_queue(
    max_messages: int = Query(default=10, ge=1, le=10),
    wait_seconds: int = Query(default=0, ge=0, le=20),
    storage: Storage = Depends(get_storage),
) -> Dict[str, Any]:
    """Receive a batch of pending comments together with their receipt handles."""
    pending = storage.moderation.receive_comments(max_messages=max_messages, wait_seconds=wait_seconds)
    return {"count": len(pending), "comments": pending}


@app.post("/moderation/comments/approve", status_code=201)
def approve_comment(
    payload: ApproveRequest,
    storage: Storage = Depends(get_storage),
) -> Dict[str, Any]:
    """Publish a pending comment and remove it from the moderation queue."""
    comment = _dump(payload.comment)
    now = utc_now_iso()
    item = {
        "post_id": comment["post_id"],
        "comment_id": comment.get("comment_id") or uuid.uuid4().hex,
        "author_name": comment["author_name"],
        "body": comment["body"],
        "submitted_at": comment.get("submitted_at") or now,
        "approved_at": now,
    }
    if payload.approved_by:
        item["approved_by"] = payload.approved_by
    stored = storage.comments.put_comment(item)
    storage.moderation.delete_comment(payload.receipt_handle)
    AUDIT.info("comment %s approved for post %s", item["comment_id"], item["post_id"])
    result = dict(stored or item)
    result["status"] = "approved"
    return result


@app.post("/moderation/comments/reject")
def reject_comment(
    payload: RejectRequest,
    storage: Storage = Depends(get_storage),
) -> Dict[str, Any]:
    """Discard a pending comment and remove it from the moderation queue."""
    storage.moderation.delete_comment(payload.receipt_handle)
    AUDIT.info("comment %s rejected (reason=%s)", payload.comment_id or "unknown", payload.reason or "unspecified")
    return {
        "comment_id": payload.comment_id,
        "status": "rejected",
        "reason": payload.reason,
        "rejected_at": utc_now_iso(),
    }


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
