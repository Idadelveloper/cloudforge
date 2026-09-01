"""Offline tests for the file-sharing backend.

All AWS access is replaced with in-memory fakes or stub boto3 clients.
"""
import os
import sys
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage  # noqa: E402


@pytest.fixture
def repo():
    return storage.InMemoryFileRepository()


@pytest.fixture
def store():
    return storage.InMemoryObjectStore(bucket="test-bucket")


@pytest.fixture
def client(repo, store):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    app_module.app.dependency_overrides[app_module.get_object_store] = lambda: store
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _request_upload(client, owner="alice", filename="report.pdf", content_type="application/pdf"):
    response = client.post(
        "/files/upload-url",
        json={"owner": owner, "filename": filename, "content_type": content_type},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload_and_confirm(client, store, owner="alice", filename="report.pdf", size=128):
    created = _request_upload(client, owner=owner, filename=filename)
    store.put_object(created["s3_key"], size)
    confirmed = client.post("/files/{0}/confirm".format(created["file_id"]))
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["s3"] is True
    assert body["dynamodb"] is True
    assert body["bucket"]
    assert body["table"]


def test_health_reports_degraded(client, repo, monkeypatch):
    monkeypatch.setattr(repo, "healthy", lambda: False)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["dynamodb"] is False


def test_create_upload_url_creates_pending_record(client, repo):
    created = _request_upload(client)
    assert created["upload_url"].startswith("https://test-bucket.s3.local/")
    assert created["expires_in_seconds"] == 900
    assert created["s3_key"] == "alice/{0}/report.pdf".format(created["file_id"])

    stored = repo.get(created["file_id"])
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["owner"] == "alice"
    assert stored["content_type"] == "application/pdf"


def test_create_upload_url_rejects_path_separator(client):
    response = client.post("/files/upload-url", json={"owner": "alice", "filename": "../etc/passwd"})
    assert response.status_code == 400


def test_create_upload_url_validation_error(client):
    response = client.post("/files/upload-url", json={"owner": "", "filename": "a.txt"})
    assert response.status_code == 422


def test_confirm_requires_object_in_s3(client):
    created = _request_upload(client)
    response = client.post("/files/{0}/confirm".format(created["file_id"]))
    assert response.status_code == 409


def test_confirm_unknown_file(client):
    assert client.post("/files/does-not-exist/confirm").status_code == 404


def test_confirm_records_size_and_status(client, store):
    confirmed = _upload_and_confirm(client, store, size=4321)
    assert confirmed["status"] == "available"
    assert confirmed["size_bytes"] == 4321
    assert confirmed["upload_time"]


def test_get_file_and_not_found(client, store):
    confirmed = _upload_and_confirm(client, store)
    response = client.get("/files/{0}".format(confirmed["file_id"]))
    assert response.status_code == 200
    assert response.json()["filename"] == "report.pdf"
    assert client.get("/files/missing").status_code == 404


def test_list_files_paginates(client, store):
    for index in range(3):
        _upload_and_confirm(client, store, filename="f{0}.bin".format(index), size=10)
    _upload_and_confirm(client, store, owner="bob", filename="other.bin", size=99)

    first = client.get("/files", params={"owner": "alice", "limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert body["owner"] == "alice"
    assert body["count"] == 2
    assert body["next_token"]

    second = client.get("/files", params={"owner": "alice", "limit": 2, "next_token": body["next_token"]})
    assert second.status_code == 200
    tail = second.json()
    assert tail["count"] == 1
    assert tail["next_token"] is None


def test_list_files_rejects_bad_token(client):
    response = client.get("/files", params={"owner": "alice", "next_token": "!!!not-base64!!!"})
    assert response.status_code == 400


def test_download_url(client, store):
    confirmed = _upload_and_confirm(client, store)
    response = client.get("/files/{0}/download-url".format(confirmed["file_id"]))
    assert response.status_code == 200
    body = response.json()
    assert "method=GET" in body["download_url"]
    assert body["expires_in_seconds"] == 900


def test_download_url_requires_confirmed_upload(client):
    created = _request_upload(client)
    response = client.get("/files/{0}/download-url".format(created["file_id"]))
    assert response.status_code == 409
    assert client.get("/files/missing/download-url").status_code == 404


def test_delete_file_removes_object_and_metadata(client, store, repo):
    confirmed = _upload_and_confirm(client, store)
    file_id = confirmed["file_id"]
    response = client.delete("/files/{0}".format(file_id))
    assert response.status_code == 200
    assert response.json() == {"file_id": file_id, "deleted": True}
    assert repo.get(file_id) is None
    assert store.objects == {}
    assert client.delete("/files/{0}".format(file_id)).status_code == 404


def test_usage_for_single_owner(client, store):
    _upload_and_confirm(client, store, size=100)
    _upload_and_confirm(client, store, filename="b.bin", size=50)
    _upload_and_confirm(client, store, owner="bob", filename="c.bin", size=7)

    response = client.get("/usage", params={"owner": "alice"})
    assert response.status_code == 200
    body = response.json()
    assert body["total_bytes"] == 150
    assert body["owners"] == [{"owner": "alice", "file_count": 2, "total_bytes": 150}]


def test_usage_for_all_owners(client, store):
    _upload_and_confirm(client, store, size=100)
    _upload_and_confirm(client, store, owner="bob", filename="c.bin", size=7)

    body = client.get("/usage").json()
    assert body["total_bytes"] == 107
    owners = {entry["owner"]: entry for entry in body["owners"]}
    assert owners["alice"]["total_bytes"] == 100
    assert owners["bob"]["file_count"] == 1


class FakeClientError(Exception):
    """Minimal stand-in for botocore ClientError."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class StubS3Client:
    """Stub boto3 S3 client - never touches the network."""

    def __init__(self, bucket_ok=True):
        self.bucket_ok = bucket_ok
        self.objects = {}
        self.calls = []

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))
        if not self.bucket_ok:
            raise FakeClientError("404")
        return {}

    def generate_presigned_url(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        return "https://example.invalid/{0}".format(kwargs["Params"]["Key"])

    def head_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self.objects:
            raise FakeClientError("404")
        return {"ContentLength": self.objects[key]}

    def delete_object(self, **kwargs):
        self.objects.pop(kwargs["Key"], None)
        return {}


def test_s3_object_store_with_stub_client():
    stub = StubS3Client()
    stub.objects["alice/1/a.bin"] = 42
    obj_store = storage.S3ObjectStore(client=stub, bucket="b1")

    assert obj_store.bucket == "b1"
    assert obj_store.healthy() is True
    assert obj_store.presigned_put_url("alice/1/a.bin", "text/plain", 900).endswith("alice/1/a.bin")
    assert obj_store.presigned_get_url("alice/1/a.bin", 900, filename="a.bin").endswith("alice/1/a.bin")
    assert obj_store.head_object("alice/1/a.bin") == {"ContentLength": 42}
    assert obj_store.head_object("nope") is None
    assert obj_store.delete_object("alice/1/a.bin") is True


def test_s3_object_store_unhealthy_and_reraise():
    stub = StubS3Client(bucket_ok=False)
    obj_store = storage.S3ObjectStore(client=stub, bucket="b1")
    assert obj_store.healthy() is False

    class Boom(StubS3Client):
        def head_object(self, **kwargs):
            raise FakeClientError("AccessDenied")

    with pytest.raises(FakeClientError):
        storage.S3ObjectStore(client=Boom(), bucket="b1").head_object("k")


class StubTable:
    """Stub boto3 DynamoDB Table resource."""

    def __init__(self):
        self.items = {}
        self.table_status = "ACTIVE"
        self.last_query = None

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item["file_id"]] = dict(item)
        return {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["file_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs):
        key = kwargs["Key"]["file_id"]
        item = self.items.setdefault(key, {"file_id": key})
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        for placeholder, attribute in names.items():
            item[attribute] = values[placeholder.replace("#k", ":v")]
        return {"Attributes": dict(item)}

    def delete_item(self, **kwargs):
        self.items.pop(kwargs["Key"]["file_id"], None)
        return {}

    def query(self, **kwargs):
        self.last_query = kwargs
        return {"Items": [dict(item) for item in self.items.values()]}

    def scan(self, **kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}


def test_dynamo_repository_round_trip():
    table = StubTable()
    repository = storage.DynamoFileRepository(table=table, index_name="owner-index")

    assert repository.healthy() is True
    repository.create(
        {
            "file_id": "f1",
            "owner": "alice",
            "filename": "a.bin",
            "content_type": "application/octet-stream",
            "size_bytes": Decimal("12"),
            "s3_key": "alice/f1/a.bin",
            "status": "pending",
            "upload_time": "2024-01-01T00:00:00Z",
            "created_at": "2024-01-01T00:00:00Z",
            "unused": None,
        }
    )
    assert "unused" not in table.items["f1"]

    fetched = repository.get("f1")
    assert fetched is not None
    assert fetched["size_bytes"] == 12
    assert repository.get("missing") is None

    updated = repository.update("f1", {"status": "available", "size_bytes": 99})
    assert updated is not None
    assert updated["status"] == "available"
    assert updated["size_bytes"] == 99
    assert repository.update("f1", {}) is not None

    items, last_key = repository.list_by_owner("alice", limit=10)
    assert len(items) == 1
    assert last_key is None
    assert table.last_query["IndexName"] == "owner-index"

    assert len(repository.all_by_owner("alice")) == 1
    assert len(repository.scan_all()) == 1
    assert repository.delete("f1") is True
    assert table.items == {}


def test_token_helpers_round_trip():
    token = storage.encode_token({"file_id": "abc", "offset": Decimal("3")})
    assert storage.decode_token(token) == {"file_id": "abc", "offset": 3}
    with pytest.raises(ValueError):
        storage.decode_token("@@@not-valid@@@")


def test_decode_values_handles_nested_decimals():
    decoded = storage.decode_values({"a": [Decimal("1"), Decimal("1.5")], "b": {"c": Decimal("2")}})
    assert decoded == {"a": [1, 1.5], "b": {"c": 2}}


def test_config_helpers(monkeypatch):
    monkeypatch.setenv("FILE_SHARE_BUCKET", "custom-bucket")
    monkeypatch.setenv("FILE_SHARE_TABLE", "custom-table")
    monkeypatch.setenv("FILE_SHARE_OWNER_INDEX", "custom-index")
    monkeypatch.setenv("PRESIGNED_URL_EXPIRY_SECONDS", "not-a-number")
    assert storage.bucket_name() == "custom-bucket"
    assert storage.table_name() == "custom-table"
    assert storage.owner_index_name() == "custom-index"
    assert storage.presign_expiry_seconds() == 900

    monkeypatch.setenv("PRESIGNED_URL_EXPIRY_SECONDS", "1")
    assert storage.presign_expiry_seconds() == 60


def test_clients_use_endpoint_env(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    assert storage.endpoint_url() == "http://localhost:4566"
    assert storage.region_name() == "us-east-1"
    assert storage.s3_client().meta.endpoint_url == "http://localhost:4566"
    assert storage.dynamodb_resource().meta.client.meta.endpoint_url == "http://localhost:4566"


def test_default_dependency_factories_are_cached():
    first_repo = app_module.get_repository()
    second_repo = app_module.get_repository()
    first_store = app_module.get_object_store()
    second_store = app_module.get_object_store()
    assert first_repo is second_repo
    assert first_store is second_store
    assert isinstance(first_repo, storage.DynamoFileRepository)
    assert isinstance(first_store, storage.S3ObjectStore)
