"""FastAPI entrypoint for the todo_api service.

Exposes CRUD endpoints for to-do tasks persisted in DynamoDB. The data access
layer is injected through FastAPI dependencies so the HTTP layer can be tested
without AWS.
"""
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query

from models import ErrorResponse, Task, TaskCreateRequest, TaskListResponse, TaskUpdateRequest
from storage import DynamoDBTaskRepository, TaskRepository, table_name

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("todo_api")

app = FastAPI(
    title="todo_api",
    version="1.0.0",
    description="REST API for managing to-do tasks stored in DynamoDB.",
)

_repository: Optional[TaskRepository] = None


def get_repository() -> TaskRepository:
    """Return the process-wide repository, creating the DynamoDB one lazily."""
    global _repository
    if _repository is None:
        _repository = DynamoDBTaskRepository()
    return _repository


def set_repository(repository: Optional[TaskRepository]) -> None:
    """Replace the process-wide repository (used by tests and local tooling)."""
    global _repository
    _repository = repository


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _payload(model: Any, exclude_unset: bool = False) -> Dict[str, Any]:
    """Dump a pydantic model to a dict for both pydantic v1 and v2."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=exclude_unset)
    return model.dict(exclude_unset=exclude_unset)


def _validate_due_date(value: Any) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="due_date must be an ISO-8601 date (YYYY-MM-DD)")
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="due_date must be an ISO-8601 date (YYYY-MM-DD)")
    return parsed.isoformat()


def _validate_description(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail="description must be a non-empty string")
    return value.strip()


def _storage_call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a repository call, converting backend failures into HTTP 503."""
    try:
        return func(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("storage backend error: %s", exc)
        raise HTTPException(status_code=503, detail="storage backend unavailable") from exc


@app.get("/")
def root() -> Dict[str, Any]:
    """Basic service description."""
    return {
        "service": "todo_api",
        "version": "1.0.0",
        "table": table_name(),
        "endpoints": [
            "POST /tasks",
            "GET /tasks",
            "GET /tasks/{task_id}",
            "PATCH /tasks/{task_id}",
            "PATCH /tasks/{task_id}/complete",
            "DELETE /tasks/{task_id}",
            "GET /health",
        ],
    }


@app.get("/health")
def health(repo: TaskRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Liveness/readiness probe reporting DynamoDB table reachability."""
    reachable = False
    try:
        reachable = bool(repo.ping())
    except Exception as exc:
        logger.warning("health check could not reach DynamoDB: %s", exc)
    return {
        "status": "ok" if reachable else "degraded",
        "table": table_name(),
        "dynamodb": "available" if reachable else "unavailable",
    }


@app.post(
    "/tasks",
    response_model=Task,
    status_code=201,
    responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def create_task(
    payload: TaskCreateRequest,
    repo: TaskRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Create a new task and store it in DynamoDB."""
    now = _utc_now()
    completed = bool(payload.completed)
    item: Dict[str, Any] = {
        "task_id": str(uuid.uuid4()),
        "description": _validate_description(payload.description),
        "due_date": _validate_due_date(payload.due_date),
        "completed": completed,
        "created_at": now,
        "updated_at": now,
        "completed_at": now if completed else None,
    }
    created = _storage_call(repo.create, item)
    logger.info("created task %s", item["task_id"])
    return created


@app.get("/tasks", response_model=TaskListResponse, responses={503: {"model": ErrorResponse}})
def list_tasks(
    completed: Optional[bool] = Query(default=None, description="Filter on completion state"),
    repo: TaskRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List all tasks, optionally filtered by completion state."""
    items = list(_storage_call(repo.list, completed))
    items.sort(key=lambda entry: (str(entry.get("due_date") or ""), str(entry.get("created_at") or "")))
    return {"items": items, "count": len(items)}


@app.get(
    "/tasks/{task_id}",
    response_model=Task,
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_task(task_id: str, repo: TaskRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Fetch a single task by id."""
    item = _storage_call(repo.get, task_id)
    if item is None:
        raise HTTPException(status_code=404, detail="task {0} not found".format(task_id))
    return item


@app.patch(
    "/tasks/{task_id}/complete",
    response_model=Task,
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def complete_task(task_id: str, repo: TaskRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Mark a task as completed."""
    now = _utc_now()
    changes = {"completed": True, "completed_at": now, "updated_at": now}
    updated = _storage_call(repo.update, task_id, changes)
    if updated is None:
        raise HTTPException(status_code=404, detail="task {0} not found".format(task_id))
    logger.info("completed task %s", task_id)
    return updated


@app.patch(
    "/tasks/{task_id}",
    response_model=Task,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def update_task(
    task_id: str,
    payload: TaskUpdateRequest,
    repo: TaskRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Update mutable fields of a task."""
    provided = _payload(payload, exclude_unset=True)
    now = _utc_now()
    changes: Dict[str, Any] = {}

    if provided.get("description") is not None:
        changes["description"] = _validate_description(provided["description"])
    if provided.get("due_date") is not None:
        changes["due_date"] = _validate_due_date(provided["due_date"])
    if provided.get("completed") is not None:
        completed = bool(provided["completed"])
        changes["completed"] = completed
        changes["completed_at"] = now if completed else None

    if not changes:
        raise HTTPException(status_code=400, detail="no updatable fields provided")

    changes["updated_at"] = now
    updated = _storage_call(repo.update, task_id, changes)
    if updated is None:
        raise HTTPException(status_code=404, detail="task {0} not found".format(task_id))
    return updated


@app.delete(
    "/tasks/{task_id}",
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def delete_task(task_id: str, repo: TaskRepository = Depends(get_repository)) -> Dict[str, Any]:
    """Delete a task by id."""
    deleted = _storage_call(repo.delete, task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="task {0} not found".format(task_id))
    logger.info("deleted task %s", task_id)
    return {"task_id": task_id, "deleted": True}


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
