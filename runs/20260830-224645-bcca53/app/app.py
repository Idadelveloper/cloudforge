"""FastAPI application exposing the to-do task REST API.

Endpoints:
    POST   /tasks                    create a task
    GET    /tasks                    list tasks (optional ?completed=true|false)
    GET    /tasks/{task_id}          fetch a single task
    PATCH  /tasks/{task_id}          partially update a task
    POST   /tasks/{task_id}/complete mark a task completed
    DELETE /tasks/{task_id}          delete a task
    GET    /health                   liveness / readiness probe

The data access layer is injected through the ``get_repository`` dependency so
the HTTP layer can be tested without touching AWS.
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from storage import (
    DEFAULT_LIMIT,
    DynamoTaskRepository,
    TaskRepository,
    new_task_id,
    table_name,
    utc_now_iso,
)

MAX_LIMIT = 500
DATE_FORMAT = "%Y-%m-%d"

app = FastAPI(
    title="todo_task_api",
    description="REST API for managing a personal to-do list backed by DynamoDB.",
    version="1.0.0",
)

_repository: Optional[TaskRepository] = None


def get_repository() -> TaskRepository:
    """Return the process-wide repository (lazily created DynamoDB backend)."""
    global _repository
    if _repository is None:
        _repository = DynamoTaskRepository()
    return _repository


class Task(BaseModel):
    """A persisted to-do task."""

    task_id: str
    description: str
    due_date: str
    completed: bool = False
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None


class TaskCreateRequest(BaseModel):
    """Payload accepted by ``POST /tasks``."""

    description: str
    due_date: str
    completed: Optional[bool] = False


class TaskUpdateRequest(BaseModel):
    """Payload accepted by ``PATCH /tasks/{task_id}``."""

    description: Optional[str] = None
    due_date: Optional[str] = None
    completed: Optional[bool] = None


class TaskListResponse(BaseModel):
    """Envelope returned by ``GET /tasks``."""

    items: List[Task] = []
    count: int = 0


class ErrorResponse(BaseModel):
    """Error body returned for 4xx/5xx responses."""

    detail: str
    code: Optional[str] = None


NOT_FOUND_RESPONSE = {404: {"model": ErrorResponse}}


def _dump(model: BaseModel) -> Dict[str, Any]:
    """Return only the fields explicitly supplied by the client."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def _validate_description(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail="description must be a non-empty string")
    return value.strip()


def _validate_due_date(value: Any) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="due_date must be an ISO-8601 date (YYYY-MM-DD)")
    candidate = value.strip()
    try:
        datetime.strptime(candidate, DATE_FORMAT)
    except ValueError:
        raise HTTPException(status_code=422, detail="due_date must be an ISO-8601 date (YYYY-MM-DD)")
    return candidate


@app.get("/health")
def health(repo: TaskRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Confirm the process is up and the datastore is reachable."""
    if not repo.healthy():
        raise HTTPException(status_code=503, detail="datastore unavailable")
    return {"status": "ok", "table": table_name()}


@app.post("/tasks", response_model=Task, status_code=201)
def create_task(
    payload: TaskCreateRequest,
    repo: TaskRepository = Depends(get_repository),
) -> Task:
    """Create a new task and return it."""
    description = _validate_description(payload.description)
    due_date = _validate_due_date(payload.due_date)
    completed = bool(payload.completed)
    now = utc_now_iso()
    item = {
        "task_id": new_task_id(),
        "description": description,
        "due_date": due_date,
        "completed": completed,
        "created_at": now,
        "updated_at": now,
        "completed_at": now if completed else None,
    }
    created = repo.create(item)
    return Task(**created)


@app.get("/tasks", response_model=TaskListResponse)
def list_tasks(
    completed: Optional[bool] = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    repo: TaskRepository = Depends(get_repository),
) -> TaskListResponse:
    """List tasks, optionally filtered by completion state."""
    items = repo.list_tasks(completed=completed, limit=limit)
    tasks = [Task(**item) for item in items]
    return TaskListResponse(items=tasks, count=len(tasks))


@app.get("/tasks/{task_id}", response_model=Task, responses=NOT_FOUND_RESPONSE)
def get_task(task_id: str, repo: TaskRepository = Depends(get_repository)) -> Task:
    """Fetch a single task by id."""
    item = repo.get(task_id)
    if item is None:
        raise HTTPException(status_code=404, detail="task not found")
    return Task(**item)


@app.patch("/tasks/{task_id}", response_model=Task, responses=NOT_FOUND_RESPONSE)
def update_task(
    task_id: str,
    payload: TaskUpdateRequest,
    repo: TaskRepository = Depends(get_repository),
) -> Task:
    """Partially update a task."""
    provided = _dump(payload)
    if not provided:
        raise HTTPException(status_code=400, detail="no updatable fields supplied")

    now = utc_now_iso()
    changes: Dict[str, Any] = {}
    if "description" in provided:
        changes["description"] = _validate_description(provided["description"])
    if "due_date" in provided:
        changes["due_date"] = _validate_due_date(provided["due_date"])
    if "completed" in provided:
        completed = bool(provided["completed"])
        changes["completed"] = completed
        changes["completed_at"] = now if completed else None
    changes["updated_at"] = now

    item = repo.update(task_id, changes)
    if item is None:
        raise HTTPException(status_code=404, detail="task not found")
    return Task(**item)


@app.post("/tasks/{task_id}/complete", response_model=Task, responses=NOT_FOUND_RESPONSE)
def complete_task(task_id: str, repo: TaskRepository = Depends(get_repository)) -> Task:
    """Mark a task as completed."""
    now = utc_now_iso()
    item = repo.update(task_id, {"completed": True, "completed_at": now, "updated_at": now})
    if item is None:
        raise HTTPException(status_code=404, detail="task not found")
    return Task(**item)


@app.delete("/tasks/{task_id}", status_code=204, responses=NOT_FOUND_RESPONSE)
def delete_task(task_id: str, repo: TaskRepository = Depends(get_repository)) -> None:
    """Delete a task."""
    if not repo.delete(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return None


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
