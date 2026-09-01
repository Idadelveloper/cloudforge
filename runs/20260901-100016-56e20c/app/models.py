"""Pydantic request/response models for the async job processing service."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class JobSubmitRequest(BaseModel):
    """Payload accepted by POST /jobs."""

    job_type: str = Field(..., min_length=1, max_length=64)
    payload: Dict[str, Any] = Field(default_factory=dict)
    priority: Optional[str] = Field(default="normal")


class JobSubmitResponse(BaseModel):
    """Response returned right after a job is queued."""

    job_id: str
    status: str
    created_at: str


class JobStatusResponse(BaseModel):
    """Lightweight polling response."""

    job_id: str
    status: str
    attempts: int = 0
    updated_at: Optional[str] = None


class JobResultResponse(BaseModel):
    """Result of a finished (successful or failed) job."""

    job_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    completed_at: Optional[str] = None


class JobListResponse(BaseModel):
    """Paginated list of job records."""

    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_token: Optional[str] = None
    count: int = 0


class DeadLetterMessage(BaseModel):
    """A single message peeked from the dead-letter queue."""

    message_id: Optional[str] = None
    job_id: Optional[str] = None
    body: Optional[Dict[str, Any]] = None
    raw_body: Optional[str] = None
    approximate_receive_count: int = 0


class DeadLetterResponse(BaseModel):
    """Response of GET /jobs/dead-letter."""

    messages: List[DeadLetterMessage] = Field(default_factory=list)
    count: int = 0


class HealthResponse(BaseModel):
    """Response of GET /healthz."""

    status: str
    table: bool
    queue: bool
    table_name: str
    queue_name: str
