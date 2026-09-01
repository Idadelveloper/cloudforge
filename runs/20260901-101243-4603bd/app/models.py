"""Pydantic models and shared constants for the job processing service."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
STATUS_DEAD_LETTER = "DEAD_LETTER"

ALLOWED_STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_DEAD_LETTER,
)

ALLOWED_PRIORITIES = ("low", "normal", "high")


class Job(BaseModel):
    """Full job record as stored in DynamoDB."""

    job_id: str
    job_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    status: str = STATUS_QUEUED
    attempts: int = 0
    priority: Optional[str] = "normal"
    created_at: str = ""
    updated_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    result: Optional[Any] = None
    result_location: Optional[str] = None
    sqs_message_id: Optional[str] = None


class JobSubmissionRequest(BaseModel):
    """Body accepted by POST /jobs."""

    job_type: str = Field(..., min_length=1, max_length=64)
    payload: Dict[str, Any]
    priority: Optional[str] = "normal"
    callback_metadata: Optional[Dict[str, Any]] = None


class JobSubmissionResponse(BaseModel):
    """Response returned by POST /jobs."""

    job_id: str
    status: str
    created_at: str


class JobStatusResponse(BaseModel):
    """Response returned by GET /jobs/{job_id}/status."""

    job_id: str
    status: str
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None


class JobResultResponse(BaseModel):
    """Response returned by GET /jobs/{job_id}/result."""

    job_id: str
    status: str
    result: Optional[Any] = None
    result_url: Optional[str] = None
    completed_at: Optional[str] = None


class JobListResponse(BaseModel):
    """Response returned by GET /jobs."""

    jobs: List[Dict[str, Any]] = Field(default_factory=list)
    count: int = 0
    next_token: Optional[str] = None


class DeadLetterMessage(BaseModel):
    """A single message sitting in the dead-letter queue."""

    message_id: str
    job_id: Optional[str] = None
    body: Optional[Any] = None
    approximate_receive_count: int = 0


class DeadLetterListResponse(BaseModel):
    """Response returned by GET /jobs/failed/dead-letter."""

    messages: List[DeadLetterMessage] = Field(default_factory=list)
    count: int = 0
