"""Offline tests for the async job processing service.

Every AWS interaction is replaced by an in-memory fake, so the suite runs
without LocalStack, credentials or network access.
"""

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

import app as app_module
import storage
import tasks
import worker


class FakeRepository(storage.JobRepository):
    """In-memory replacement for the DynamoDB repository."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.table_name = "fake-jobs"
        self.fail_ping = False
        self.fail_writes = False

    def create_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        if self.fail_writes:
            raise RuntimeError("dynamodb unavailable")
        self.items[job["job_id"]] = dict(job)
        return dict(job)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        item = self.items.get(job_id)
        return dict(item) if item else None

    def update_job(self, job_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        item = self.items.setdefault(job_id, {"job_id": job_id})
        item.update(updates)
        return dict(item)

    def list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 25,
        next_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        items = [dict(item) for item in self.items.values()]
        if status:
            items = [item for item in items if item.get("status") == status]
        page = items[:limit]
        token = "more" if len(items) > limit else None
        return page, token

    def ping(self) -> bool:
        if self.fail_ping:
            raise RuntimeError("table unreachable")
        return True


class FakeQueue(storage.JobQueue):
    """In-memory replacement for the SQS queue."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.dead_letter: List[Dict[str, Any]] = []
        self.queue_name = "fake-queue"
        self.fail_send = False

    def send_job(self, message: Dict[str, Any]) -> str:
        if self.fail_send:
            raise RuntimeError("sqs unavailable")
        self.sent.append(dict(message))
        return "msg-%d" % len(self.sent)

    def peek_dead_letter(self, max_messages: int = 10) -> List[Dict[str, Any]]:
        return self.dead_letter[:max_messages]

    def ping(self) -> bool:
        return True


class FakeDynamoTable:
    """Minimal stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.last_query: Optional[Dict[str, Any]] = None
        self.last_scan: Optional[Dict[str, Any]] = None
        self.table_status = "ACTIVE"

    def put_item(self, Item):
        self.items[Item["job_id"]] = Item
        return {}

    def get_item(self, Key):
        item = self.items.get(Key["job_id"])
        return {"Item": item} if item else {}

    def update_item(self, **kwargs):
        key = kwargs["Key"]
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        item = self.items.setdefault(key["job_id"], dict(key))
        for placeholder, attribute in names.items():
            item[attribute] = values[":v" + placeholder[2:]]
        return {"Attributes": item}

    def query(self, **kwargs):
        self.last_query = kwargs
        return {
            "Items": list(self.items.values()),
            "LastEvaluatedKey": {"job_id": "cursor"},
        }

    def scan(self, **kwargs):
        self.last_scan = kwargs
        return {"Items": list(self.items.values())}


class FakeDynamoResource:
    """Resource stub returning a single fake table."""

    def __init__(self, table: FakeDynamoTable) -> None:
        self._table = table
        self.requested_name: Optional[str] = None

    def Table(self, name):
        self.requested_name = name
        return self._table


class FakeSqsClient:
    """Minimal stand-in for a boto3 SQS client."""

    def __init__(self) -> None:
        self.sent: List[Tuple[str, str]] = []
        self.receive_kwargs: Optional[Dict[str, Any]] = None

    def get_queue_url(self, QueueName):
        return {"QueueUrl": "https://sqs.local/%s" % QueueName}

    def send_message(self, QueueUrl, MessageBody, **kwargs):
        self.sent.append((QueueUrl, MessageBody))
        return {"MessageId": "m-1"}

    def receive_message(self, **kwargs):
        self.receive_kwargs = kwargs
        return {
            "Messages": [
                {
                    "MessageId": "dlq-1",
                    "Body": json.dumps({"job_id": "job-1", "job_type": "sum"}),
                    "Attributes": {"ApproximateReceiveCount": "3"},
                },
                {
                    "MessageId": "dlq-2",
                    "Body": "not-json",
                    "Attributes": {},
                },
            ]
        }

    def get_queue_attributes(self, **kwargs):
        return {"Attributes": {"QueueArn": "arn:aws:sqs:us-east-1:000000000000:q"}}


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def client(repo, queue):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    app_module.app.dependency_overrides[app_module.get_queue] = lambda: queue
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _submit(client, job_type="sum", payload=None):
    body = {"job_type": job_type, "payload": payload if payload is not None else {"values": [1, 2, 3]}}
    return client.post("/jobs", json=body)


def test_submit_job_creates_record_and_enqueues(client, repo, queue):
    response = _submit(client)
    assert response.status_code == 201
    body = response.json()
    job_id = body["job_id"]
    assert body["status"] == "QUEUED"
    assert body["created_at"]
    assert repo.items[job_id]["status"] == "QUEUED"
    assert queue.sent[0]["job_id"] == job_id
    assert queue.sent[0]["job_type"] == "sum"


def test_submit_job_rejects_unknown_type(client):
    response = _submit(client, job_type="mine-bitcoin")
    assert response.status_code == 400
    assert "unsupported job_type" in response.json()["detail"]


def test_submit_job_validation_error(client):
    response = client.post("/jobs", json={"payload": {}})
    assert response.status_code == 422


def test_submit_job_enqueue_failure_marks_failed(client, repo, queue):
    queue.fail_send = True
    response = _submit(client)
    assert response.status_code == 503
    assert len(repo.items) == 1
    stored = list(repo.items.values())[0]
    assert stored["status"] == "FAILED"
    assert "failed to enqueue" in stored["error"]


def test_submit_job_store_failure(client, repo):
    repo.fail_writes = True
    response = _submit(client)
    assert response.status_code == 503


def test_get_job_and_not_found(client):
    job_id = _submit(client).json()["job_id"]
    response = client.get("/jobs/%s" % job_id)
    assert response.status_code == 200
    assert response.json()["job_id"] == job_id
    assert client.get("/jobs/does-not-exist").status_code == 404


def test_status_endpoint(client, repo):
    job_id = _submit(client).json()["job_id"]
    repo.update_job(job_id, {"status": "RUNNING", "attempts": 1, "updated_at": "2024-01-01T00:00:00Z"})
    response = client.get("/jobs/%s/status" % job_id)
    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "job_id": job_id,
        "status": "RUNNING",
        "attempts": 1,
        "updated_at": "2024-01-01T00:00:00Z",
    }
    assert client.get("/jobs/nope/status").status_code == 404


def test_result_conflict_then_success(client, repo):
    job_id = _submit(client).json()["job_id"]
    conflict = client.get("/jobs/%s/result" % job_id)
    assert conflict.status_code == 409

    repo.update_job(
        job_id,
        {
            "status": "SUCCEEDED",
            "result": {"sum": 6},
            "completed_at": "2024-01-01T00:00:05Z",
        },
    )
    ok = client.get("/jobs/%s/result" % job_id)
    assert ok.status_code == 200
    assert ok.json()["result"] == {"sum": 6}
    assert ok.json()["completed_at"] == "2024-01-01T00:00:05Z"


def test_result_for_failed_job_returns_error(client, repo):
    job_id = _submit(client).json()["job_id"]
    repo.update_job(job_id, {"status": "FAILED", "error": "boom"})
    response = client.get("/jobs/%s/result" % job_id)
    assert response.status_code == 200
    assert response.json()["error"] == "boom"
    assert client.get("/jobs/unknown/result").status_code == 404


def test_list_jobs_with_and_without_filter(client, repo):
    first = _submit(client).json()["job_id"]
    second = _submit(client).json()["job_id"]
    repo.update_job(second, {"status": "SUCCEEDED"})

    unfiltered = client.get("/jobs")
    assert unfiltered.status_code == 200
    assert unfiltered.json()["count"] == 2

    filtered = client.get("/jobs", params={"status": "succeeded"})
    assert filtered.status_code == 200
    body = filtered.json()
    assert body["count"] == 1
    assert body["items"][0]["job_id"] == second
    assert first != second


def test_list_jobs_pagination_token(client):
    _submit(client)
    _submit(client)
    response = client.get("/jobs", params={"limit": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["next_token"] == "more"


def test_list_jobs_invalid_status(client):
    response = client.get("/jobs", params={"status": "BOGUS"})
    assert response.status_code == 400


def test_dead_letter_endpoint(client, queue):
    queue.dead_letter = [
        {
            "message_id": "dlq-1",
            "job_id": "job-1",
            "body": {"job_id": "job-1"},
            "raw_body": None,
            "approximate_receive_count": 2,
        }
    ]
    response = client.get("/jobs/dead-letter")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["messages"][0]["job_id"] == "job-1"


def test_retry_endpoint(client, repo, queue):
    job_id = _submit(client).json()["job_id"]
    conflict = client.post("/jobs/%s/retry" % job_id)
    assert conflict.status_code == 409

    repo.update_job(job_id, {"status": "FAILED", "error": "boom", "attempts": 2})
    response = client.post("/jobs/%s/retry" % job_id)
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"
    assert repo.items[job_id]["error"] is None
    assert len(queue.sent) == 2

    assert client.post("/jobs/missing/retry").status_code == 404


def test_retry_enqueue_failure(client, repo, queue):
    job_id = _submit(client).json()["job_id"]
    repo.update_job(job_id, {"status": "DEAD_LETTER"})
    queue.fail_send = True
    response = client.post("/jobs/%s/retry" % job_id)
    assert response.status_code == 503


def test_healthz_ok_and_degraded(client, repo):
    healthy = client.get("/healthz")
    assert healthy.status_code == 200
    body = healthy.json()
    assert body["status"] == "ok"
    assert body["table"] is True
    assert body["queue"] is True
    assert body["table_name"] == "fake-jobs"

    repo.fail_ping = True
    degraded = client.get("/healthz").json()
    assert degraded["status"] == "degraded"
    assert degraded["table"] is False


def test_tasks_execute_all_supported_types():
    assert tasks.execute_job("sum", {"values": [1, 2, 3]})["sum"] == 6
    assert tasks.execute_job("multiply", {"values": [2, 3]})["product"] == 6
    assert tasks.execute_job("uppercase", {"text": "abc"}) == {"text": "ABC", "length": 3}
    assert tasks.execute_job("fibonacci", {"n": 10}) == {"n": 10, "value": "55"}
    assert tasks.execute_job("echo", {"a": 1}) == {"echo": {"a": 1}}


@pytest.mark.parametrize(
    "job_type,payload",
    [
        ("sum", {"values": []}),
        ("sum", {"values": ["x"]}),
        ("uppercase", {"text": ""}),
        ("fibonacci", {"n": "abc"}),
        ("fibonacci", {"n": 99999}),
        ("unknown", {}),
    ],
)
def test_tasks_invalid_payloads(job_type, payload):
    with pytest.raises(ValueError):
        tasks.execute_job(job_type, payload)


def _sqs_event(job_id, job_type="sum", payload=None, receive_count=1, body=None):
    message_body = body
    if message_body is None:
        message_body = json.dumps(
            {
                "job_id": job_id,
                "job_type": job_type,
                "payload": payload if payload is not None else {"values": [1, 2, 3]},
                "submitted_at": "2024-01-01T00:00:00Z",
            }
        )
    return {
        "Records": [
            {
                "messageId": "m-1",
                "body": message_body,
                "attributes": {"ApproximateReceiveCount": str(receive_count)},
            }
        ]
    }


def test_worker_success(repo):
    repo.create_job({"job_id": "job-1", "status": "QUEUED", "attempts": 0})
    result = worker.handler(_sqs_event("job-1"), None, repo=repo)
    assert result["processed"] == 1
    stored = repo.get_job("job-1")
    assert stored["status"] == "SUCCEEDED"
    assert stored["result"]["sum"] == 6
    assert stored["attempts"] == 1
    assert stored["completed_at"]


def test_worker_first_failure_marks_failed(repo):
    repo.create_job({"job_id": "job-2", "status": "QUEUED", "attempts": 0})
    event = _sqs_event("job-2", job_type="sum", payload={"values": []}, receive_count=1)
    with pytest.raises(RuntimeError):
        worker.handler(event, None, repo=repo)
    stored = repo.get_job("job-2")
    assert stored["status"] == "FAILED"
    assert stored["completed_at"] is None
    assert stored["error"]


def test_worker_second_failure_marks_dead_letter(repo):
    repo.create_job({"job_id": "job-3", "status": "QUEUED", "attempts": 0})
    event = _sqs_event("job-3", job_type="sum", payload={"values": []}, receive_count=2)
    with pytest.raises(RuntimeError):
        worker.handler(event, None, repo=repo)
    stored = repo.get_job("job-3")
    assert stored["status"] == "DEAD_LETTER"
    assert stored["attempts"] == 2
    assert stored["completed_at"]


def test_worker_malformed_and_empty_events(repo):
    with pytest.raises(RuntimeError):
        worker.handler(_sqs_event("job-4", body="{not json"), None, repo=repo)
    with pytest.raises(RuntimeError):
        worker.handler(_sqs_event("", body=json.dumps({"job_type": "sum"})), None, repo=repo)
    assert worker.handler({}, None, repo=repo) == {"processed": 0, "results": []}


def test_worker_uses_default_repository(monkeypatch, repo):
    repo.create_job({"job_id": "job-5", "status": "QUEUED"})
    monkeypatch.setattr(worker, "get_repository", lambda: repo)
    worker.handler(_sqs_event("job-5"))
    assert repo.get_job("job-5")["status"] == "SUCCEEDED"


def test_worker_max_receive_count_env(monkeypatch):
    monkeypatch.setenv("MAX_RECEIVE_COUNT", "5")
    assert worker.max_receive_count() == 5
    monkeypatch.setenv("MAX_RECEIVE_COUNT", "not-a-number")
    assert worker.max_receive_count() == 2


def test_to_native_and_to_dynamo():
    native = storage.to_native({"a": Decimal("3"), "b": [Decimal("1.5")], "c": "x"})
    assert native == {"a": 3, "b": [1.5], "c": "x"}
    converted = storage.to_dynamo({"v": 1.25})
    assert isinstance(converted["v"], Decimal)


def test_pagination_token_roundtrip():
    token = storage.encode_token({"job_id": "abc"})
    assert storage.decode_token(token) == {"job_id": "abc"}
    assert storage.encode_token(None) is None
    assert storage.decode_token(None) is None
    with pytest.raises(ValueError):
        storage.decode_token("!!!not-valid!!!")


def test_dynamo_repository_with_fake_resource():
    table = FakeDynamoTable()
    repository = storage.DynamoJobRepository(table_name="t", resource=FakeDynamoResource(table))
    repository.create_job(
        {"job_id": "j1", "status": "QUEUED", "attempts": 0, "payload": {"values": [1.5]}}
    )
    stored = repository.get_job("j1")
    assert stored["status"] == "QUEUED"
    assert stored["payload"]["values"] == [1.5]
    assert repository.get_job("missing") is None

    updated = repository.update_job("j1", {"status": "SUCCEEDED", "attempts": 1})
    assert updated["status"] == "SUCCEEDED"
    assert updated["attempts"] == 1
    assert repository.update_job("j1", {})["status"] == "SUCCEEDED"

    items, token = repository.list_jobs(status="SUCCEEDED", limit=10)
    assert len(items) == 1
    assert token is not None
    assert table.last_query["IndexName"] == storage.STATUS_INDEX

    items, token = repository.list_jobs(next_token=token)
    assert token is None
    assert table.last_scan["ExclusiveStartKey"] == {"job_id": "cursor"}
    assert repository.ping() is True


def test_sqs_queue_with_fake_client(monkeypatch):
    monkeypatch.delenv("JOBS_QUEUE_URL", raising=False)
    monkeypatch.delenv("JOBS_DLQ_URL", raising=False)
    fake = FakeSqsClient()
    queue = storage.SqsJobQueue(client=fake)

    message_id = queue.send_job({"job_id": "job-1"})
    assert message_id == "m-1"
    url, body = fake.sent[0]
    assert url.endswith(storage.DEFAULT_QUEUE_NAME)
    assert json.loads(body)["job_id"] == "job-1"

    messages = queue.peek_dead_letter(max_messages=5)
    assert messages[0]["job_id"] == "job-1"
    assert messages[0]["approximate_receive_count"] == 3
    assert messages[1]["body"] is None
    assert messages[1]["raw_body"] == "not-json"
    assert fake.receive_kwargs["MaxNumberOfMessages"] == 5
    assert queue.ping() is True


def test_sqs_queue_uses_env_urls(monkeypatch):
    monkeypatch.setenv("JOBS_QUEUE_URL", "https://sqs.local/env-queue")
    monkeypatch.setenv("JOBS_DLQ_URL", "https://sqs.local/env-dlq")
    queue = storage.SqsJobQueue(client=FakeSqsClient())
    assert queue.queue_url == "https://sqs.local/env-queue"
    assert queue.dlq_url == "https://sqs.local/env-dlq"


def test_client_factories_honour_endpoint_override(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "local")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local")
    assert storage.aws_endpoint_url() == "http://localhost:4566"
    assert storage.aws_region() == "us-east-1"
    assert storage.sqs_client().meta.endpoint_url == "http://localhost:4566"
    assert storage.dynamodb_resource().meta.client.meta.endpoint_url == "http://localhost:4566"


def test_app_dependency_factories_are_cached(monkeypatch):
    monkeypatch.setattr(app_module, "_repository", None)
    monkeypatch.setattr(app_module, "_queue", None)
    monkeypatch.setattr(app_module, "DynamoJobRepository", FakeRepository)
    monkeypatch.setattr(app_module, "SqsJobQueue", FakeQueue)
    assert app_module.get_repository() is app_module.get_repository()
    assert app_module.get_queue() is app_module.get_queue()
    monkeypatch.setattr(app_module, "_repository", None)
    monkeypatch.setattr(app_module, "_queue", None)
