# todo_task_api

A small FastAPI service that manages a personal to-do list. Tasks are persisted
in a single DynamoDB table keyed by a server-generated `task_id`.

## Endpoints

| Method | Path                        | Description                                   |
| ------ | --------------------------- | --------------------------------------------- |
| GET    | `/health`                   | Liveness probe (also checks the table)        |
| POST   | `/tasks`                    | Create a task (`description`, `due_date`)     |
| GET    | `/tasks`                    | List tasks (`?completed=true|false`, `?limit`) |
| GET    | `/tasks/{task_id}`          | Fetch one task (404 if unknown)               |
| PATCH  | `/tasks/{task_id}`          | Partially update description/due_date/completed |
| POST   | `/tasks/{task_id}/complete` | Mark the task completed                       |
| DELETE | `/tasks/{task_id}`          | Delete the task (204, or 404 if unknown)      |

`GET /tasks` returns `{"items": [...], "count": N}`.

## Configuration

| Variable              | Default       | Purpose                                     |
| --------------------- | ------------- | ------------------------------------------- |
| `TASKS_TABLE_NAME`    | `todo_tasks`  | DynamoDB table name                         |
| `AWS_ENDPOINT_URL`    | *(unset)*     | Point boto3 at LocalStack, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION`  | `us-east-1`   | AWS region                                  |
| `HOST` / `PORT`       | `127.0.0.1` / `8000` | Bind address for the dev entrypoint  |

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # optional (LocalStack)
export TASKS_TABLE_NAME=todo_tasks
uvicorn app:app --host 127.0.0.1 --port 8000
```

Interactive docs are available at `http://127.0.0.1:8000/docs`.

## Example

```bash
curl -X POST http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"description": "Buy milk", "due_date": "2030-01-31"}'
```

## Tests

The test suite runs completely offline; all AWS calls are stubbed.

```bash
pip install pytest
pytest
```
