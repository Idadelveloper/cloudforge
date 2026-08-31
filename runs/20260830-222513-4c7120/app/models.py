"""Pydantic request/response models for the todo_api service."""
from typing import List, Optional

from pydantic import BaseModel, Field


class TaskCreateRequest(BaseModel):
    """Body of POST /tasks."""

    description: str = Field(..., description="What needs to be done")
    due_date: str = Field(..., description="ISO-8601 date (YYYY-MM-DD)")
    completed: bool = Field(default=False, description="Initial completion state")


class TaskUpdateRequest(BaseModel):
    """Body of PATCH /tasks/{task_id}. All fields optional."""

    description: Optional[str] = None
    due_date: Optional[str] = None
    completed: Optional[bool] = None


class Task(BaseModel):
    """A stored task record."""

    task_id: str
    description: str
    due_date: str
    completed: bool = False
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None


class TaskListResponse(BaseModel):
    """Response of GET /tasks."""

    items: List[Task]
    count: int


class ErrorResponse(BaseModel):
    """Standard error payload."""

    detail: str
