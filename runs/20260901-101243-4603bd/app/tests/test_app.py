"""Offline tests for the async job processing API and Lambda worker.

All AWS access is replaced by in-memory fakes; no network or LocalStack
instance is required.
"""

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import worker as worker_module  # noqa: E402
from storage import JobNotFoundError, JobStateConflictError, decode_item, encode_item  # noqa: E402


class FakeRepository:
    """In-memory stand-in for DynamoJobRepository."""

    def __init__(self, healthy=True):
        self.items = {}
        self.healthy = healthy

    def create_job(self, item):
        self.items[item["job_id"]] = dict(item)
        return dict(item)

    def get_job(self, job_id):
        item = self.items.get(job_id)
        return dict(item) if item is not None else None

    def update_job(self, job_id, updates, expected_statuses=None):
        item = self.items.get(job_id)
        if item is None:
            raise JobNotFoundError(job_id)
        if expected_statuses and item.get("status") not in expected_statuses:
            raise JobStateConflictError(job_id)
        for key, value in updates.items():
            if value is not None and key != "job_id":
                item[key] = value
        return dict(item)

    def list_jobs(self, status=None, limit=25, start_key=None):
        items = sorted(self.items.values(), key=lambda i: i.get("created_at", ""), reverse=True)
        if status:
            items = [i for i in items if i.get("status") == status]
        page = [dict(i) for i in items[:limit]]
        last_key = {"job_id": page[-1]["job_id"]} if len(items) > limit and page else None
        return page, last_key

    def ping(self):
        if not self.healthy:
            raise RuntimeError("dynamodb unavailable")
        return True


class FakeQueue:
    """In-memory stand-in for SqsJobQueue."""

    def __init__(self, healthy=True, send_fails=False):
        self.sent = []
        self.dead = []
        self.healthy = healthy
        self.send_fails = send_fails

    def send_job(self, message):
        if self.send_fails:
            raise RuntimeError("sqs unavailable")
        self.sent.append(message)
        return "msg-%d" % len(self.sent)

    def receive_dead_letters(self, max_messages=10):
        if not self.healthy:
            raise RuntimeError("dlq unavailable")
        return self.dead[:max_messages]

    def ping(self):
        if not self.healthy:
            raise RuntimeError("sqs unavailable")
        return True


class FakeResultStore:
    """In-memory stand-in for S3ResultStore."""

    def __init__(self):
        self.objects = {}

    def put_json(self, key, document):
        self.objects[key] = document
        return key

    def presigned_url(self, key, expires_in=None):
        return "https://results.example.com/%s" % key


@pytest.fixture
def fakes():
    repo = FakeRepository()
    queue = FakeQueue()
    store = FakeResultStore()
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    app_module.app.dependency_overrides[app_module.get_queue] = lambda: queue
    app_module.app.dependency_overrides[app_module.get_result_store] = lambda: store
    yield repo, queue, store
    app_module.app.dependency_overrides.clear()


@pytest.fixture
def client(fakes):
    with TestClient(app_module.app) as test_client:
        yield test_client


def submit(client, job_type="sum", payload=None, priority="normal"):
    body = {"job_type": job_type, "payload": payload or {"numbers": [1, 2, 3]}, "priority": priority}
    return client.post("/jobs", json=body)


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_degraded(fakes):
    repo, queue, _store = fakes
    queue.healthy = False
    with TestClient(app_module.app) as local_client:
        response = local_client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["sqs"] == "unavailable"
    assert repo.ping() is True


def test_submit_job_creates_record_and_enqueues(client, fakes):
    repo, queue, _store = fakes
    response = submit(client)
    assert response.status_code == 201
    body = response.json()
    job_id = body["job_id"]
    assert body["status"] == "QUEUED"
    assert body["created_at"]
    assert job_id in repo.items
    assert repo.items[job_id]["sqs_message_id"] == "msg-1"
    assert queue.sent[0]["job_id"] == job_id


def test_submit_job_rejects_bad_priority(client):
    response = submit(client, priority="urgent")
    assert response.status_code == 400
    assert "priority" in response.json()["detail"]


def test_submit_job_validation_error(client):
    response = client.post("/jobs", json={"payload": {}})
    assert response.status_code == 422


def test_submit_job_queue_failure_marks_failed(fakes):
    repo, queue, _store = fakes
    queue.send_fails = True
    with TestClient(app_module.app) as local_client:
        response = submit(local_client)
    assert response.status_code == 503
    assert len(repo.items) == 1
    stored = list(repo.items.values())[0]
    assert stored["status"] == "FAILED"


def test_get_job(client):
    job_id = submit(client).json()["job_id"]
    response = client.get("/jobs/%s" % job_id)
    assert response.status_code == 200
    assert response.json()["job_type"] == "sum"


def test_get_job_not_found(client):
    response = client.get("/jobs/does-not-exist")
    assert response.status_code == 404


def test_get_job_status(client, fakes):
    repo, _queue, _store = fakes
    job_id = submit(client).json()["job_id"]
    repo.items[job_id]["attempts"] = 1
    response = client.get("/jobs/%s/status" % job_id)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["attempts"] == 1


def test_get_job_status_not_found(client):
    assert client.get("/jobs/nope/status").status_code == 404


def test_result_conflict_while_running(client, fakes):
    repo, _queue, _store = fakes
    job_id = submit(client).json()["job_id"]
    repo.items[job_id]["status"] = "RUNNING"
    response = client.get("/jobs/%s/result" % job_id)
    assert response.status_code == 409


def test_result_inline(client, fakes):
    repo, _queue, _store = fakes
    job_id = submit(client).json()["job_id"]
    repo.items[job_id].update(
        {"status": "SUCCEEDED", "result": {"sum": 6}, "completed_at": "2024-01-01T00:00:00Z"}
    )
    response = client.get("/jobs/%s/result" % job_id)
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == {"sum": 6}
    assert body["result_url"] is None


def test_result_presigned_url(client, fakes):
    repo, _queue, _store = fakes
    job_id = submit(client).json()["job_id"]
    repo.items[job_id].update({"status": "SUCCEEDED", "result_location": "results/%s.json" % job_id})
    response = client.get("/jobs/%s/result" % job_id)
    assert response.status_code == 200
    assert response.json()["result_url"].endswith("%s.json" % job_id)


def test_result_not_found(client):
    assert client.get("/jobs/unknown/result").status_code == 404


def test_list_jobs_and_status_filter(client, fakes):
    repo, _queue, _store = fakes
    first = submit(client).json()["job_id"]
    second = submit(client).json()["job_id"]
    repo.items[second]["status"] = "SUCCEEDED"

    response = client.get("/jobs")
    assert response.status_code == 200
    assert response.json()["count"] == 2

    filtered = client.get("/jobs", params={"status": "succeeded"})
    assert filtered.status_code == 200
    body = filtered.json()
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == second
    assert first in repo.items


def test_list_jobs_invalid_status(client):
    response = client.get("/jobs", params={"status": "nope"})
    assert response.status_code == 400


def test_list_jobs_invalid_token(client):
    response = client.get("/jobs", params={"next_token": "!!!not-base64!!!"})
    assert response.status_code == 400


def test_list_jobs_pagination_token(client, fakes):
    repo, _queue, _store = fakes
    for _ in range(3):
        submit(client)
    response = client.get("/jobs", params={"limit": 2})
    body = response.json()
    assert body["count"] == 2
    assert body["next_token"]
    follow_up = client.get("/jobs", params={"limit": 2, "next_token": body["next_token"]})
    assert follow_up.status_code == 200
    assert len(repo.items) == 3


def test_cancel_queued_job(client, fakes):
    repo, _queue, _store = fakes
    job_id = submit(client).json()["job_id"]
    response = client.delete("/jobs/%s" % job_id)
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert repo.items[job_id]["status"] == "CANCELLED"


def test_cancel_running_job_conflict(client, fakes):
    repo, _queue, _store = fakes
    job_id = submit(client).json()["job_id"]
    repo.items[job_id]["status"] = "RUNNING"
    response = client.delete("/jobs/%s" % job_id)
    assert response.status_code == 409


def test_cancel_unknown_job(client):
    assert client.delete("/jobs/unknown").status_code == 404


def test_cancel_is_idempotent(client, fakes):
    repo, _queue, _store = fakes
    job_id = submit(client).json()["job_id"]
    client.delete("/jobs/%s" % job_id)
    response = client.delete("/jobs/%s" % job_id)
    assert response.status_code == 200
    assert repo.items[job_id]["status"] == "CANCELLED"


def test_dead_letter_listing(client, fakes):
    _repo, queue, _store = fakes
    queue.dead = [
        {
            "message_id": "m-1",
            "job_id": "job-1",
            "body": {"job_id": "job-1", "job_type": "sum"},
            "approximate_receive_count": 2,
        }
    ]
    response = client.get("/jobs/failed/dead-letter")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["messages"][0]["job_id"] == "job-1"


def test_dead_letter_unavailable(fakes):
    _repo, queue, _store = fakes
    queue.healthy = False
    with TestClient(app_module.app) as local_client:
        response = local_client.get("/jobs/failed/dead-letter")
    assert response.status_code == 503


def make_event(job_id, job_type="sum", payload=None, receive_count=1):
    body = {"job_id": job_id, "job_type": job_type, "payload": payload or {"numbers": [1, 2, 3]}}
    return {
        "Records": [
            {
                "messageId": "m-1",
                "body": json.dumps(body),
                "attributes": {"ApproximateReceiveCount": str(receive_count)},
            }
        ]
    }


@pytest.fixture
def worker_env(monkeypatch):
    repo = FakeRepository()
    store = FakeResultStore()
    monkeypatch.setattr(worker_module, "get_repository", lambda: repo)
    monkeypatch.setattr(worker_module, "get_result_store", lambda: store)
    return repo, store


def seed_job(repo, job_id="job-1", job_type="sum", payload=None, status="QUEUED"):
    repo.create_job(
        {
            "job_id": job_id,
            "job_type": job_type,
            "payload": payload or {"numbers": [1, 2, 3]},
            "status": status,
            "attempts": 0,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }
    )
    return job_id


def test_worker_success_inline(worker_env):
    repo, _store = worker_env
    job_id = seed_job(repo)
    outcome = worker_module.lambda_handler(make_event(job_id), None)
    assert outcome["count"] == 1
    stored = repo.items[job_id]
    assert stored["status"] == "SUCCEEDED"
    assert stored["result"] == {"sum": 6, "count": 3}
    assert stored["completed_at"]


def test_worker_large_result_goes_to_s3(worker_env, monkeypatch):
    repo, store = worker_env
    monkeypatch.setattr(worker_module, "MAX_INLINE_RESULT_BYTES", 5)
    job_id = seed_job(repo, job_type="echo", payload={"text": "x" * 64})
    worker_module.lambda_handler(make_event(job_id, job_type="echo", payload={"text": "x" * 64}), None)
    stored = repo.items[job_id]
    assert stored["status"] == "SUCCEEDED"
    assert stored["result_location"] == "results/%s.json" % job_id
    assert "results/%s.json" % job_id in store.objects


def test_worker_first_failure_marks_failed_and_raises(worker_env):
    repo, _store = worker_env
    job_id = seed_job(repo, job_type="unknown")
    event = make_event(job_id, job_type="unknown", receive_count=1)
    with pytest.raises(RuntimeError):
        worker_module.lambda_handler(event, None)
    assert repo.items[job_id]["status"] == "FAILED"
    assert repo.items[job_id]["attempts"] == 1


def test_worker_final_failure_marks_dead_letter(worker_env):
    repo, _store = worker_env
    job_id = seed_job(repo, job_type="sum", payload={"numbers": "not-a-list"})
    event = make_event(job_id, job_type="sum", payload={"numbers": "not-a-list"}, receive_count=2)
    with pytest.raises(RuntimeError):
        worker_module.lambda_handler(event, None)
    assert repo.items[job_id]["status"] == "DEAD_LETTER"
    assert repo.items[job_id]["error_message"]


def test_worker_skips_cancelled_job(worker_env):
    repo, _store = worker_env
    job_id = seed_job(repo, status="CANCELLED")
    result = worker_module.lambda_handler(make_event(job_id), None)
    assert result["processed"][0]["outcome"] == "SKIPPED_CANCELLED"
    assert repo.items[job_id]["status"] == "CANCELLED"


def test_worker_skips_unknown_job(worker_env):
    _repo, _store = worker_env
    result = worker_module.lambda_handler(make_event("missing"), None)
    assert result["processed"][0]["outcome"] == "SKIPPED_UNKNOWN"


def test_worker_handlers_cover_all_job_types():
    assert worker_module.execute_job("multiply", {"factors": [2, 3]})["product"] == 6
    assert worker_module.execute_job("word_count", {"text": "a b c"})["word_count"] == 3
    assert worker_module.execute_job("echo", {"k": "v"}) == {"echo": {"k": "v"}}
    with pytest.raises(ValueError):
        worker_module.execute_job("nope", {})


def test_encode_decode_roundtrip():
    encoded = encode_item({"a": 1.5, "b": [1, 2.25], "c": {"d": "x"}})
    decoded = decode_item(encoded)
    assert decoded == {"a": 1.5, "b": [1, 2.25], "c": {"d": "x"}}
