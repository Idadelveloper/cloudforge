# todo_api

A FastAPI REST service for managing to-do tasks. Tasks are persisted in a single
DynamoDB table (`tasks` by default) accessed directly with boto3.

## Endpoints

| Method | Path                          | Purpose                                        |
| ------ | ----------------------------- | ---------------------------------------------- |
| POST   | `/tasks`                      | Create a task (`description`, `due_date`)      |
| GET    | `/tasks`                      | List tasks, optional `?completed=true/false`   |
| GET    | `/tasks/{task_id}`            | Fetch one task (404 when missing)              |
| PATCH  | `/tasks/{task_id}/complete`   | Mark a task completed                          |
| PATCH  | `/tasks/{task_id}`            | Update description / due_date / completed      |
| DELETE | `/tasks/{task_id}`            | Delete a task (404 when missing)               |
| GET    | `/health`                     | Liveness probe + DynamoDB reachability         |
| GET    | `/`                           | Service metadata                               |

## Configuration

| Variable            | Default       | Meaning                                       |
| ------------------- | ------------- | --------------------------------------------- |
| `AWS_ENDPOINT_URL`  | _unset_       | Set to e.g. `http://localhost:4566` for LocalStack |
| `AWS_REGION`        | `us-east-1`   | AWS region (also honours `AWS_DEFAULT_REGION`) |
| `TASKS_TABLE`       | `tasks`       | DynamoDB table name (partition key `task_id`) |
| `HOST` / `PORT`     | `127.0.0.1` / `8000` | Bind address when run via `python app.py` |
| `LOG_LEVEL`         | `INFO`        | Root log level                                 |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AWS_ENDPOINT_URL=http://localhost:4566   # LocalStack
export AWS_REGION=us-east-1
export TASKS_TABLE=tasks
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test

uvicorn app:app --host 0.0.0.0 --port 8000
```

Interactive docs are served at `http://localhost:8000/docs`.

## Example

```bash
curl -X POST localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{"description":"buy milk","due_date":"2030-02-01"}'

curl localhost:8000/tasks
curl -X PATCH localhost:8000/tasks/<task_id>/complete
curl -X DELETE localhost:8000/tasks/<task_id>
```

## Tests

Tests run fully offline: the DynamoDB layer is replaced with an in-memory
repository through FastAPI dependency overrides, and boto3 is monkeypatched for
the repository unit tests.

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```
