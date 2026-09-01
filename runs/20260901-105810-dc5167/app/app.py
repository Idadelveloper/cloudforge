"""FastAPI application exposing the blog platform backend HTTP API.

Posts (markdown bodies + metadata) live in DynamoDB, images live in S3 and
reader comments are queued on SQS for moderation before being published.
"""
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import AwsBlogRepository, BlogRepository, PendingCommentNotFound, PostNotFound
from uploads import parse_upload

LOGGER = logging.getLogger("blog_platform_backend")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

VALID_STATUSES = ("draft", "published")
MAX_IMAGE_BYTES = int(os.environ.get("BLOG_MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))

app = FastAPI(
    title="Blog Platform Backend",
    version="1.0.0",
    description="Posts in DynamoDB, images in S3, reader comments moderated through SQS.",
)

_repository: Optional[BlogRepository] = None


def get_repository() -> BlogRepository:
    """Return the process-wide repository, building the AWS-backed one on first use."""
    global _repository
    if _repository is None:
        _repository = AwsBlogRepository()
    return _repository


class PostCreate(BaseModel):
    """Payload accepted by ``POST /posts``."""

    title: str = Field(..., min_length=1, max_length=300)
    body_markdown: str = Field(default="", max_length=200000)
    author: Optional[str] = Field(default=None, max_length=200)
    tags: List[str] = Field(default_factory=list)
    status: str = Field(default="draft")


class PostUpdate(BaseModel):
    """Payload accepted by ``PUT /posts/{post_id}``; every field is optional."""

    title: Optional[str] = Field(default=None, max_length=300)
    body_markdown: Optional[str] = Field(default=None, max_length=200000)
    author: Optional[str] = Field(default=None, max_length=200)
    tags: Optional[List[str]] = None
    status: Optional[str] = None


class CommentSubmission(BaseModel):
    """Reader supplied comment, enqueued for moderation."""

    author_name: str = Field(..., min_length=1, max_length=120)
    body: str = Field(..., min_length=1, max_length=5000)
    author_email: Optional[str] = Field(default=None, max_length=320)


class PendingCommentPayload(BaseModel):
    """The pending comment as returned by the moderation listing endpoint."""

    comment_id: Optional[str] = None
    post_id: Optional[str] = None
    author_name: Optional[str] = None
    author_email: Optional[str] = None
    body: Optional[str] = None
    submitted_at: Optional[str] = None


class ModerationDecision(BaseModel):
    """Approve/reject decision for a queued comment."""

    receipt_handle: str = Field(..., min_length=1)
    comment_id: Optional[str] = None
    moderator: Optional[str] = Field(default=None, max_length=200)
    reason: Optional[str] = Field(default=None, max_length=1000)
    comment: Optional[PendingCommentPayload] = None


def _dump(model: BaseModel) -> Dict[str, Any]:
    """Return a plain dict for a pydantic v1 or v2 model."""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _validate_status(status: Optional[str]) -> None:
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="status must be one of: {}".format(", ".join(VALID_STATUSES)),
        )


@app.exception_handler(PostNotFound)
async def _handle_post_not_found(request: Request, exc: PostNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "post not found: {}".format(exc)})


@app.exception_handler(PendingCommentNotFound)
async def _handle_pending_not_found(request: Request, exc: PendingCommentNotFound) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"detail": "pending comment not found: {} (include the full comment payload)".format(exc)},
    )


@app.get("/health")
def health(repo: BlogRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Report reachability of DynamoDB, S3 and SQS."""
    return repo.health()


@app.post("/posts", status_code=201)
def create_post(
    payload: PostCreate,
    request: Request,
    repo: BlogRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Create a post item in the posts table."""
    _validate_status(payload.status)
    data = _dump(payload)
    data["author"] = payload.author or request.headers.get("X-Author") or "anonymous"
    post = repo.create_post(data)
    LOGGER.info("created post %s", post.get("post_id"))
    return post


@app.get("/posts")
def list_posts(
    limit: int = Query(default=20, ge=1, le=100),
    next_token: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    repo: BlogRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List post summaries with an opaque pagination cursor."""
    _validate_status(status)
    try:
        return repo.list_posts(limit=limit, next_token=next_token, status=status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/posts/{post_id}")
def get_post(post_id: str, repo: BlogRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Return a single post including its markdown body and image keys."""
    post = repo.get_post(post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="post not found: {}".format(post_id))
    return post


@app.put("/posts/{post_id}")
def update_post(
    post_id: str,
    payload: PostUpdate,
    repo: BlogRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Update title, body, author, tags or status of an existing post."""
    _validate_status(payload.status)
    changes = {key: value for key, value in _dump(payload).items() if value is not None}
    if not changes:
        raise HTTPException(status_code=400, detail="no updatable fields supplied")
    return repo.update_post(post_id, changes)


@app.delete("/posts/{post_id}")
def delete_post(post_id: str, repo: BlogRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Delete a post and its S3 image objects."""
    removed = repo.delete_post(post_id)
    return {"post_id": post_id, "deleted": True, "deleted_image_keys": removed}


@app.post("/posts/{post_id}/images", status_code=201)
async def upload_image(
    post_id: str,
    request: Request,
    repo: BlogRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Upload an image (multipart form data or a raw binary body) to S3."""
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty request body")
    if len(body) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image exceeds maximum allowed size")
    parsed = parse_upload(request.headers.get("content-type", ""), body)
    if parsed is None:
        raise HTTPException(status_code=400, detail="no file part found in the upload")
    filename, content_type, data = parsed
    filename = request.headers.get("x-filename") or filename
    if not data:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    image = repo.add_image(post_id, filename, content_type, data)
    LOGGER.info("stored image %s for post %s", image.get("image_key"), post_id)
    return image


@app.get("/posts/{post_id}/images")
def list_images(post_id: str, repo: BlogRepository = Depends(get_repository)) -> Dict[str, Any]:
    """List a post's images with time limited presigned download URLs."""
    images = repo.list_images(post_id)
    return {"post_id": post_id, "count": len(images), "items": images}


@app.post("/posts/{post_id}/comments", status_code=202)
def submit_comment(
    post_id: str,
    payload: CommentSubmission,
    repo: BlogRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Send a reader comment to the SQS moderation queue."""
    pending = repo.submit_comment(post_id, _dump(payload))
    response: Dict[str, Any] = {"status": "pending_moderation"}
    response.update(pending)
    return response


@app.get("/posts/{post_id}/comments")
def list_comments(post_id: str, repo: BlogRepository = Depends(get_repository)) -> Dict[str, Any]:
    """List published (approved) comments for a post."""
    items = repo.list_comments(post_id)
    return {"post_id": post_id, "count": len(items), "items": items}


@app.get("/moderation/comments")
def list_pending_comments(
    max_messages: int = Query(default=10, ge=1, le=10),
    visibility_timeout: int = Query(default=30, ge=0, le=43200),
    wait_seconds: int = Query(default=0, ge=0, le=20),
    repo: BlogRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Poll the moderation queue and return pending comments with receipt handles."""
    items = repo.receive_pending_comments(
        max_messages=max_messages,
        visibility_timeout=visibility_timeout,
        wait_seconds=wait_seconds,
    )
    return {"count": len(items), "items": items}


@app.post("/moderation/comments/approve")
def approve_comment(
    decision: ModerationDecision,
    request: Request,
    repo: BlogRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Publish a pending comment and delete its moderation message."""
    moderator = decision.moderator or request.headers.get("X-Moderator")
    comment = _dump(decision.comment) if decision.comment is not None else None
    published = repo.approve_comment(
        receipt_handle=decision.receipt_handle,
        comment_id=decision.comment_id,
        moderator=moderator,
        comment=comment,
    )
    return {"status": "approved", "comment": published}


@app.post("/moderation/comments/reject")
def reject_comment(
    decision: ModerationDecision,
    request: Request,
    repo: BlogRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Discard a pending comment by deleting its moderation message."""
    moderator = decision.moderator or request.headers.get("X-Moderator")
    return repo.reject_comment(
        receipt_handle=decision.receipt_handle,
        comment_id=decision.comment_id,
        moderator=moderator,
        reason=decision.reason,
    )


def main() -> None:  # pragma: no cover - manual entrypoint
    """Run the service with uvicorn."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
