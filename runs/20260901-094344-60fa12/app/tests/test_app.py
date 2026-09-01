"""Offline tests for the asynchronous job-processing service."""
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module  # noqa: E402
import storage  # noqa: E402
import worker  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeRepository:
    """In-memory stand-in for :class:`storage.JobRepository`."""

    def __init__(self):
        self.settings = storage.Settings()
        self.jobs = {}
        self.results = {}
        self.sent = []
        self.dlq = []
        self.deleted_receipts = []
        self.alerts = []
        self.healthy = True
        self.enqueue_error = None
        self._counter = 0

    def create_job(self, job):
        self.jobs[job["job_id"]] = dict(job)
        return dict(job)

    def find_by_idempotency_key(self, key):
        for job in self.jobs.values():
            if job.get("idempotency_key") == key:
                return dict(job)
        return None

    def get_job(self, job_id):
        job = self.jobs.get(job_id)
        return dict(job) if job else None

    def update_job(self, job_id, updates):
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        for key, value in updates.items():
            if value is None:
                job.pop(key, None)
            else:
                job[key] = value
        return dict(job)

    def delete_job(self, job_id):
        return self.jobs.pop(job_id, None) is not None

    def list_jobs(self, status=None, limit=25, cursor=None):
        items = [dict(job) for job in self.jobs.values()]
        if status:
            items = [job for job in items if job.get("status") == status]
        items.sort(key=lambda job: job.get("created_at", ""), reverse=True)
        start = int(cursor) if cursor else 0
        page = items[start:start + limit]
        next_cursor = str(start + limit) if start + limit < len(items) else None
        return page, next_cursor

    def get_result(self, job_id):
        record = self.results.get(job_id)
        return dict(record) if record else None

    def put_result(self, job_id, result=None, error_message=None, error_type=None, duration_ms=0):
        item = {
            "job_id": job_id,
            "duration_ms": int(duration_ms),
            "completed_at": "2024-01-01T00:00:00Z",
            "result_size_bytes": len(json.dumps(result if result is not None else {})),
        }
        if result is not None:
            item["result"] = result
        if error_message:
            item["error_message"] = error_message
        if error_type:
            item["error_type"] = error_type
        self.results[job_id] = item
        return dict(item)

    def presigned_result_url(self, key):
        return "https://example.invalid/{0}".format(key)

    def enqueue_job(self, job):
        if self.enqueue_error:
            raise RuntimeError(self.enqueue_error)
        self._counter += 1
        message_id = "msg-{0}".format(self._counter)
        self.sent.append({"message_id": message_id, "job_id": job["job_id"]})
        return message_id

    def receive_dead_letters(self, max_messages=10):
        return [dict(entry) for entry in self.dlq[:max_messages]]

    def delete_dead_letter(self, receipt_handle):
        self.deleted_receipts.append(receipt_handle)
        self.dlq = [e for e in self.dlq if e.get("receipt_handle") != receipt_handle]
        return True

    def publish_failure(self, job_id, error_message):
        self.alerts.append((job_id, error_message))
        return "alert-1"

    def health(self):
        return {"dynamodb": self.healthy, "sqs": self.healthy}


class StubTable:
    def __init__(self, name):
        self.name = name
        self.items = {}
        self.last_scan_kwargs = None
        self.last_query_kwargs = None
        self.scan_last_key = None

    def put_item(self, Item=None, **kwargs):
        self.items[Item["job_id"]] = dict(Item)
        return {}

    def get_item(self, Key=None, **kwargs):
        item = self.items.get(Key["job_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key=None, UpdateExpression="", ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, **kwargs):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        item = dict(self.items.get(Key["job_id"], {"job_id": Key["job_id"]}))
        remove_clause = UpdateExpression.split("REMOVE", 1)[1] if "REMOVE" in UpdateExpression else ""
        for placeholder, attribute in names.items():
            value_key = ":v" + placeholder[2:]
            if value_key in values:
                item[attribute] = values[value_key]
            elif placeholder in remove_clause:
                item.pop(attribute, None)
        self.items[Key["job_id"]] = item
        return {"Attributes": dict(item)}

    def delete_item(self, Key=None, **kwargs):
        removed = self.items.pop(Key["job_id"], None)
        return {"Attributes": removed} if removed else {}

    def scan(self, **kwargs):
        self.last_scan_kwargs = kwargs
        response = {"Items": [dict(item) for item in self.items.values()]}
        if self.scan_last_key:
            response["LastEvaluatedKey"] = self.scan_last_key
        return response

    def query(self, **kwargs):
        self.last_query_kwargs = kwargs
        return {"Items": [dict(item) for item in self.items.values()]}


class StubMetaClient:
    def __init__(self):
        self.described = []

    def describe_table(self, TableName=None):
        self.described.append(TableName)
        return {"Table": {"TableStatus": "ACTIVE"}}


class StubMeta:
    def __init__(self):
        self.client = StubMetaClient()


class StubDynamo:
    def __init__(self):
        self.tables = {}
        self.meta = StubMeta()

    def Table(self, name):
        return self.tables.setdefault(name, StubTable(name))


class StubSQS:
    def __init__(self):
        self.sent = []
        self.messages = []
        self.deleted = []

    def get_queue_url(self, QueueName=None):
        return {"QueueUrl": "http://localhost:4566/000000000000/{0}".format(QueueName)}

    def send_message(self, QueueUrl=None, MessageBody=None):
        self.sent.append((QueueUrl, MessageBody))
        return {"MessageId": "stub-message-id"}

    def receive_message(self, **kwargs):
        return {"Messages": self.messages}

    def delete_message(self, QueueUrl=None, ReceiptHandle=None):
        self.deleted.append(ReceiptHandle)
        return {}

    def get_queue_attributes(self, **kwargs):
        return {"Attributes": {"ApproximateNumberOfMessages": "0"}}


class StubS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        self.objects[(Bucket, Key)] = Body
        return {}

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):
        return "https://s3.invalid/{0}?op={1}&exp={2}".format(Params["Key"], operation, ExpiresIn)


class StubSNS:
    def __init__(self):
        self.published = []

    def publish(self, TopicArn=None, Subject=None, Message=None):
        self.published.append((TopicArn, Subject, Message))
        return {"MessageId": "sns-1"}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def repo():
    return FakeRepository()


@pytest.fixture()
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    app_module.app.dependency_overrides[app_module.get_api_token] = lambda: None
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def submit(client, job_type="sum", payload=None, **extra):
    body = {"job_type": job_type, "payload": payload if payload is not None else {"values": [1, 2, 3]}}
    body.update(extra)
    return client.post("/jobs", json=body)


# --------------------------------------------------------------------------- #
# API tests
# --------------------------------------------------------------------------- #
def test_healthz_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_degraded(client, repo):
    repo.healthy = False
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_submit_job_creates_record_and_enqueues(client, repo):
    response = submit(client)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["attempts"] == 0
    assert body["sqs_message_id"] == "msg-1"
    assert body["job_id"] in repo.jobs
    assert repo.sent[0]["job_id"] == body["job_id"]


def test_submit_job_validation_error(client):
    response = client.post("/jobs", json={"payload": {}})
    assert response.status_code == 422


def test_submit_job_is_idempotent(client):
    first = submit(client, idempotency_key="key-1")
    second = submit(client, idempotency_key="key-1")
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]


def test_submit_job_enqueue_failure_marks_failed(client, repo):
    repo.enqueue_error = "sqs unavailable"
    response = submit(client)
    assert response.status_code == 502
    assert len(repo.jobs) == 1
    stored = list(repo.jobs.values())[0]
    assert stored["status"] == "FAILED"
    assert "failed to enqueue" in stored["error_message"]


def test_auth_required_when_credential_configured(repo):
    configured_credential = "unit-test-credential"
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    app_module.app.dependency_overrides[app_module.get_api_token] = lambda: configured_credential
    with TestClient(app_module.app) as authed:
        unauthorized = authed.post("/jobs", json={"job_type": "echo", "payload": {}})
        assert unauthorized.status_code == 401
        authorized = authed.post(
            "/jobs",
            json={"job_type": "echo", "payload": {}},
            headers={"Authorization": "Bearer " + configured_credential},
        )
        assert authorized.status_code == 201
    app_module.app.dependency_overrides.clear()


def test_get_job_and_404(client):
    job_id = submit(client).json()["job_id"]
    found = client.get("/jobs/" + job_id)
    assert found.status_code == 200
    assert found.json()["job_id"] == job_id
    assert client.get("/jobs/does-not-exist").status_code == 404


def test_status_endpoint(client, repo):
    job_id = submit(client).json()["job_id"]
    repo.jobs[job_id]["attempts"] = 1
    response = client.get("/jobs/{0}/status".format(job_id))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["attempts"] == 1
    assert body["max_attempts"] >= 1
    assert client.get("/jobs/missing/status").status_code == 404


def test_result_conflict_then_success(client, repo):
    job_id = submit(client).json()["job_id"]
    pending = client.get("/jobs/{0}/result".format(job_id))
    assert pending.status_code == 409

    repo.jobs[job_id]["status"] = "SUCCEEDED"
    repo.put_result(job_id, result={"sum": 6.0}, duration_ms=12)
    done = client.get("/jobs/{0}/result".format(job_id))
    assert done.status_code == 200
    body = done.json()
    assert body["result"] == {"sum": 6.0}
    assert body["duration_ms"] == 12


def test_result_missing_job_and_s3_pointer(client, repo):
    assert client.get("/jobs/unknown/result").status_code == 404

    job_id = submit(client).json()["job_id"]
    repo.jobs[job_id]["status"] = "SUCCEEDED"
    repo.results[job_id] = {
        "job_id": job_id,
        "result_s3_key": "results/{0}.json".format(job_id),
        "result_size_bytes": 999999,
        "duration_ms": 42,
        "completed_at": "2024-01-01T00:00:00Z",
    }
    response = client.get("/jobs/{0}/result".format(job_id))
    assert response.status_code == 200
    assert response.json()["result_url"].endswith("results/{0}.json".format(job_id))


def test_result_conflict_when_record_missing(client, repo):
    job_id = submit(client).json()["job_id"]
    repo.jobs[job_id]["status"] = "SUCCEEDED"
    response = client.get("/jobs/{0}/result".format(job_id))
    assert response.status_code == 409


def test_list_jobs_filter_and_pagination(client, repo):
    first = submit(client).json()["job_id"]
    second = submit(client).json()["job_id"]
    repo.jobs[second]["status"] = "SUCCEEDED"

    all_jobs = client.get("/jobs")
    assert all_jobs.status_code == 200
    assert all_jobs.json()["count"] == 2

    filtered = client.get("/jobs", params={"status": "SUCCEEDED"})
    assert [item["job_id"] for item in filtered.json()["items"]] == [second]

    paged = client.get("/jobs", params={"limit": 1})
    assert paged.json()["count"] == 1
    assert paged.json()["next_cursor"] == "1"
    page_two = client.get("/jobs", params={"limit": 1, "cursor": "1"})
    assert page_two.json()["count"] == 1
    assert {first, second} == {
        paged.json()["items"][0]["job_id"],
        page_two.json()["items"][0]["job_id"],
    }

    assert client.get("/jobs", params={"status": "BOGUS"}).status_code == 400


def test_delete_cancels_queued_job(client, repo):
    job_id = submit(client).json()["job_id"]
    response = client.delete("/jobs/" + job_id)
    assert response.status_code == 200
    assert response.json() == {"job_id": job_id, "status": "CANCELED", "deleted": False}
    assert repo.jobs[job_id]["status"] == "CANCELED"


def test_delete_removes_terminal_job_and_rejects_running(client, repo):
    job_id = submit(client).json()["job_id"]
    repo.jobs[job_id]["status"] = "RUNNING"
    assert client.delete("/jobs/" + job_id).status_code == 409

    repo.jobs[job_id]["status"] = "SUCCEEDED"
    response = client.delete("/jobs/" + job_id)
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert job_id not in repo.jobs
    assert client.delete("/jobs/" + job_id).status_code == 404


def test_dead_letter_listing(client, repo):
    repo.dlq.append(
        {
            "job_id": "job-1",
            "message_id": "m-1",
            "receipt_handle": "rh-1",
            "body": json.dumps({"job_id": "job-1"}),
            "approximate_receive_count": 3,
            "first_seen_at": "2024-01-01T00:00:00Z",
        }
    )
    response = client.get("/dead-letters")
    assert response.status_code == 200
    entries = response.json()
    assert entries[0]["job_id"] == "job-1"
    assert entries[0]["approximate_receive_count"] == 3


def test_replay_dead_letter(client, repo):
    job_id = submit(client).json()["job_id"]
    repo.jobs[job_id].update({"status": "DEAD_LETTER", "attempts": 2, "error_message": "boom"})
    repo.dlq.append(
        {
            "job_id": job_id,
            "message_id": "m-9",
            "receipt_handle": "rh-9",
            "body": json.dumps({"job_id": job_id}),
            "approximate_receive_count": 2,
            "first_seen_at": "2024-01-01T00:00:00Z",
        }
    )

    response = client.post("/dead-letters/{0}/replay".format(job_id))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["dlq_messages_removed"] == 1
    assert repo.deleted_receipts == ["rh-9"]
    assert repo.jobs[job_id]["attempts"] == 0
    assert "error_message" not in repo.jobs[job_id]
    assert len(repo.sent) == 2


def test_replay_unknown_job(client):
    assert client.post("/dead-letters/nope/replay").status_code == 404


# --------------------------------------------------------------------------- #
# Worker tests
# --------------------------------------------------------------------------- #
def _record(job_id, receive_count=1, message_id="m1", payload=None, job_type="sum"):
    body = {"job_id": job_id, "job_type": job_type, "payload": payload or {"values": [1, 2, 3]}}
    return {
        "messageId": message_id,
        "receiptHandle": "rh",
        "body": json.dumps(body),
        "attributes": {"ApproximateReceiveCount": str(receive_count)},
    }


def _seed(repo, job_type="sum", payload=None, status="QUEUED"):
    job = {
        "job_id": "job-abc",
        "job_type": job_type,
        "payload": payload if payload is not None else {"values": [1, 2, 3]},
        "status": status,
        "attempts": 0,
        "max_attempts": 2,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    repo.create_job(job)
    return job


def test_worker_success(repo):
    job = _seed(repo)
    output = worker.lambda_handler({"Records": [_record(job["job_id"])]}, None, repo=repo)
    assert output == {"batchItemFailures": []}
    assert repo.jobs[job["job_id"]]["status"] == "SUCCEEDED"
    assert repo.results[job["job_id"]]["result"]["sum"] == 6.0


def test_worker_failure_first_attempt_is_retried(repo):
    job = _seed(repo, job_type="unknown-type")
    output = worker.lambda_handler({"Records": [_record(job["job_id"], 1)]}, None, repo=repo)
    assert output["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    assert repo.jobs[job["job_id"]]["status"] == "FAILED"
    assert repo.alerts == []


def test_worker_failure_final_attempt_dead_letters(repo):
    job = _seed(repo, job_type="unknown-type")
    output = worker.lambda_handler({"Records": [_record(job["job_id"], 2)]}, None, repo=repo)
    assert output["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    assert repo.jobs[job["job_id"]]["status"] == "DEAD_LETTER"
    assert repo.results[job["job_id"]]["error_type"] == "ValueError"
    assert repo.alerts and repo.alerts[0][0] == job["job_id"]


def test_worker_skips_unknown_canceled_and_bad_messages(repo):
    unknown = worker.lambda_handler({"Records": [_record("missing")]}, None, repo=repo)
    assert unknown["batchItemFailures"] == []

    job = _seed(repo, status="CANCELED")
    canceled = worker.lambda_handler({"Records": [_record(job["job_id"])]}, None, repo=repo)
    assert canceled["batchItemFailures"] == []
    assert repo.jobs[job["job_id"]]["status"] == "CANCELED"

    bad = worker.lambda_handler(
        {"Records": [{"messageId": "m2", "body": "not-json"}, {"messageId": "m3", "body": "{}"}]},
        None,
        repo=repo,
    )
    assert bad["batchItemFailures"] == []


def test_execute_job_variants():
    assert worker.execute_job("echo", {"a": 1}) == {"echo": {"a": 1}}
    assert worker.execute_job("uppercase", {"text": "ab"}) == {"text": "AB", "length": 2}
    assert worker.execute_job("wordcount", {"text": "a b c"})["words"] == 3
    assert worker.execute_job("sum", {"values": [2, 3]})["sum"] == 5.0
    with pytest.raises(ValueError):
        worker.execute_job("sum", {"values": []})
    with pytest.raises(ValueError):
        worker.execute_job("sum", {"values": ["x"]})
    with pytest.raises(ValueError):
        worker.execute_job("uppercase", {"text": 5})
    with pytest.raises(ValueError):
        worker.execute_job("wordcount", {})
    with pytest.raises(ValueError):
        worker.execute_job("nope", {})


# --------------------------------------------------------------------------- #
# Storage tests
# --------------------------------------------------------------------------- #
def test_dynamo_conversions_roundtrip():
    data = {"a": 1.5, "b": [1, 2], "c": {"d": 3}}
    stored = storage.to_dynamo(data)
    assert isinstance(stored["a"], Decimal)
    assert storage.from_dynamo(stored) == data
    assert storage.from_dynamo({"s": {Decimal("2")}}) == {"s": [2]}


def test_cursor_roundtrip_and_error():
    cursor = storage.encode_cursor({"job_id": "abc"})
    assert storage.decode_cursor(cursor) == {"job_id": "abc"}
    with pytest.raises(ValueError):
        storage.decode_cursor("@@@not-a-cursor@@@")


def test_clients_use_endpoint_and_region(monkeypatch):
    captured = {}

    def fake_client(service, **kwargs):
        captured[service] = kwargs
        return "client:" + service

    def fake_resource(service, **kwargs):
        captured[service] = kwargs
        return "resource:" + service

    monkeypatch.setattr(storage.boto3, "client", fake_client)
    monkeypatch.setattr(storage.boto3, "resource", fake_resource)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    storage.dynamodb_resource()
    storage.sqs_client()
    storage.s3_client()
    storage.sns_client()
    storage.secrets_client()

    for service in ("dynamodb", "sqs", "s3", "sns", "secretsmanager"):
        assert captured[service]["endpoint_url"] == "http://localhost:4566"
        assert captured[service]["region_name"] == "us-east-1"


def _stub_repo():
    dynamo, sqs, s3, sns = StubDynamo(), StubSQS(), StubS3(), StubSNS()
    repository = storage.JobRepository(dynamodb=dynamo, sqs=sqs, s3=s3, sns=sns)
    repository.settings.queue_url = ""
    repository.settings.dlq_url = ""
    repository.settings.failure_topic_arn = "arn:aws:sns:us-east-1:000000000000:job-failure-alerts"
    return repository, dynamo, sqs, s3, sns


def test_repository_job_lifecycle_with_stubs():
    repository, dynamo, sqs, _s3, sns = _stub_repo()
    job = {
        "job_id": "j-1",
        "job_type": "echo",
        "payload": {"a": 1},
        "status": storage.STATUS_QUEUED,
        "attempts": 0,
        "max_attempts": 2,
        "created_at": storage.utcnow(),
        "updated_at": storage.utcnow(),
        "idempotency_key": "idem-1",
        "error_message": "old",
    }
    repository.create_job(job)
    assert repository.get_job("j-1")["job_type"] == "echo"

    updated = repository.update_job("j-1", {"status": storage.STATUS_RUNNING, "error_message": None})
    assert updated["status"] == storage.STATUS_RUNNING
    assert "error_message" not in updated
    assert repository.update_job("j-1", {}) ["job_id"] == "j-1"

    message_id = repository.enqueue_job(job)
    assert message_id == "stub-message-id"
    assert sqs.sent[0][0].endswith("job-queue")

    dynamo.Table("jobs").scan_last_key = {"job_id": "j-1"}
    items, cursor = repository.list_jobs()
    assert len(items) == 1 and cursor
    dynamo.Table("jobs").scan_last_key = None

    queried, _ = repository.list_jobs(status=storage.STATUS_RUNNING, cursor=cursor)
    assert len(queried) == 1
    assert dynamo.Table("jobs").last_query_kwargs["IndexName"] == repository.settings.status_index

    assert repository.publish_failure("j-1", "boom") == "sns-1"
    assert sns.published

    checks = repository.health()
    assert checks["dynamodb"] is True and checks["sqs"] is True

    assert repository.delete_job("j-1") is True
    assert repository.get_job("j-1") is None
    assert repository.delete_job("j-1") is False


def test_repository_results_inline_and_s3():
    repository, _dynamo, _sqs, s3, _sns = _stub_repo()
    inline = repository.put_result("j-2", result={"ok": True}, duration_ms=7)
    assert inline["result"] == {"ok": True}
    assert repository.get_result("j-2")["duration_ms"] == 7

    repository.settings.inline_result_limit = 10
    overflow = repository.put_result("j-3", result={"blob": "x" * 100}, duration_ms=3)
    assert overflow["result_s3_key"] == "results/j-3.json"
    assert (repository.settings.results_bucket, "results/j-3.json") in s3.objects
    url = repository.presigned_result_url("results/j-3.json")
    assert "results/j-3.json" in url

    failed = repository.put_result("j-4", error_message="boom", error_type="ValueError")
    assert failed["error_message"] == "boom"
    assert failed["result_size_bytes"] == 0


def test_repository_dead_letters_with_stubs():
    repository, _dynamo, sqs, _s3, _sns = _stub_repo()
    sqs.messages = [
        {
            "MessageId": "m-1",
            "ReceiptHandle": "rh-1",
            "Body": json.dumps({"job_id": "j-9"}),
            "Attributes": {"ApproximateReceiveCount": "2", "SentTimestamp": "1700000000000"},
        },
        {"MessageId": "m-2", "ReceiptHandle": "rh-2", "Body": "not-json", "Attributes": {}},
    ]
    entries = repository.receive_dead_letters(5)
    assert entries[0]["job_id"] == "j-9"
    assert entries[0]["approximate_receive_count"] == 2
    assert entries[0]["first_seen_at"].endswith("Z")
    assert entries[1]["job_id"] == ""
    assert entries[1]["first_seen_at"] is None

    assert repository.delete_dead_letter("rh-1") is True
    assert sqs.deleted == ["rh-1"]


def test_repository_finds_by_idempotency_key():
    repository, _dynamo, _sqs, _s3, _sns = _stub_repo()
    repository.create_job({"job_id": "j-5", "idempotency_key": "abc", "status": "QUEUED"})
    assert repository.find_by_idempotency_key("abc")["job_id"] == "j-5"


def test_get_repository_is_cached():
    storage.reset_repository()
    first = storage.get_repository()
    assert storage.get_repository() is first
    storage.reset_repository()


def test_get_api_token_from_environment(monkeypatch):
    storage.reset_token_cache()
    monkeypatch.setenv("API_AUTH_TOKEN", "env-value")
    assert storage.get_api_token() == "env-value"
    storage.reset_token_cache()


def test_get_api_token_from_secrets_manager(monkeypatch):
    storage.reset_token_cache()
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)

    class FakeSecrets:
        def get_secret_value(self, SecretId=None):
            return {"SecretString": json.dumps({"api_token": "from-secrets"})}

    monkeypatch.setattr(storage, "secrets_client", lambda: FakeSecrets())
    assert storage.get_api_token() == "from-secrets"
    assert storage.get_api_token() == "from-secrets"
    storage.reset_token_cache()


def test_get_api_token_missing_returns_none(monkeypatch):
    storage.reset_token_cache()
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)

    def boom():
        raise RuntimeError("secrets manager unreachable")

    monkeypatch.setattr(storage, "secrets_client", boom)
    assert storage.get_api_token() is None
    storage.reset_token_cache()
