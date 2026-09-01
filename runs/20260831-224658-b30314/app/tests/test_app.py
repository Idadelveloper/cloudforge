"""Offline tests for the file sharing backend (no AWS or network access)."""

import os
import sys
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage as storage_module  # noqa: E402


class FakeClientError(Exception):
    """Minimal stand-in for botocore ClientError."""

    def __init__(self, code: str) -> None:
        super().__init__("aws error {0}".format(code))
        self.response = {"Error": {"Code": code}}


class FakeS3(object):
    def __init__(self) -> None:
        self.sizes: Dict[str, int] = {}
        self.deleted: List[str] = []
        self.presign_calls: List[Dict[str, Any]] = []
        self.head_error: Optional[Exception] = None
        self.delete_error: Optional[Exception] = None

    def generate_presigned_url(self, ClientMethod, Params=None, ExpiresIn=None):
        params = Params or {}
        self.presign_calls.append(
            {"method": ClientMethod, "params": params, "expires_in": ExpiresIn}
        )
        return "https://s3.example.test/{0}?op={1}".format(params.get("Key", ""), ClientMethod)

    def head_object(self, Bucket=None, Key=None):
        if self.head_error is not None:
            raise self.head_error
        if Key not in self.sizes:
            raise FakeClientError("404")
        return {"ContentLength": self.sizes[Key]}

    def delete_object(self, Bucket=None, Key=None):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(Key)
        return {}


class FakeTable(object):
    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.query_responses: List[Dict[str, Any]] = []
        self.scan_responses: List[Dict[str, Any]] = []
        self.query_calls: List[Dict[str, Any]] = []
        self.scan_calls: List[Dict[str, Any]] = []
        self.deleted: List[str] = []

    def put_item(self, Item=None, **kwargs):
        self.items[Item["file_id"]] = dict(Item)
        return {}

    def get_item(self, Key=None, **kwargs):
        item = self.items.get(Key["file_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key=None, ExpressionAttributeValues=None, **kwargs):
        item = self.items.setdefault(Key["file_id"], {"file_id": Key["file_id"]})
        values = ExpressionAttributeValues or {}
        item["status"] = values.get(":st", item.get("status"))
        item["size_bytes"] = values.get(":sz", item.get("size_bytes"))
        item["uploaded_at"] = values.get(":ts", item.get("uploaded_at"))
        return {"Attributes": dict(item)}

    def delete_item(self, Key=None, **kwargs):
        self.deleted.append(Key["file_id"])
        self.items.pop(Key["file_id"], None)
        return {}

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if self.query_responses:
            return self.query_responses.pop(0)
        return {"Items": []}

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        if self.scan_responses:
            return self.scan_responses.pop(0)
        return {"Items": []}


class FakeStore(storage_module.FileStore):
    """In-memory store injected into the API for endpoint tests."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.counter = 0
        self.raise_storage_error = False

    def _maybe_fail(self) -> None:
        if self.raise_storage_error:
            raise storage_module.StorageError("dynamodb unavailable")

    def create_upload_url(self, owner, filename, content_type="application/octet-stream", size_bytes=None):
        self._maybe_fail()
        self.counter += 1
        file_id = "file-{0}".format(self.counter)
        s3_key = "{0}/{1}/{2}".format(owner, file_id, filename)
        self.items[file_id] = {
            "file_id": file_id,
            "owner": owner,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": int(size_bytes or 0),
            "s3_key": s3_key,
            "status": "pending",
            "uploaded_at": "2024-01-01T00:00:00Z",
        }
        return {
            "file_id": file_id,
            "upload_url": "https://s3.example.test/{0}".format(s3_key),
            "s3_key": s3_key,
            "expires_in": 900,
        }

    def _require(self, file_id: str) -> Dict[str, Any]:
        self._maybe_fail()
        item = self.items.get(file_id)
        if item is None:
            raise storage_module.NotFoundError("file {0} not found".format(file_id))
        return item

    def complete_upload(self, file_id):
        item = self._require(file_id)
        item["status"] = "available"
        item["size_bytes"] = 2048
        item["uploaded_at"] = "2024-01-02T00:00:00Z"
        return dict(item)

    def get_file(self, file_id):
        return dict(self._require(file_id))

    def download_url(self, s3_key):
        return "https://s3.example.test/{0}?download=1".format(s3_key)

    def list_files(self, owner, limit=25, next_token=None):
        self._maybe_fail()
        if next_token == "bad":
            raise storage_module.InvalidTokenError("invalid next_token")
        rows = [dict(i) for i in self.items.values() if i["owner"] == owner]
        rows.sort(key=lambda row: row["file_id"])
        start = 0
        if next_token:
            start = int(next_token)
        page = rows[start:start + limit]
        token = str(start + limit) if len(rows) > start + limit else None
        return page, token

    def delete_file(self, file_id):
        item = self._require(file_id)
        self.items.pop(file_id, None)
        return dict(item)

    def usage(self, owner=None):
        self._maybe_fail()
        totals: Dict[str, List[int]] = {}
        if owner:
            totals[owner] = [0, 0]
        for item in self.items.values():
            if owner and item["owner"] != owner:
                continue
            entry = totals.setdefault(item["owner"], [0, 0])
            entry[0] += 1
            entry[1] += int(item["size_bytes"])
        owners = [
            {"owner": name, "file_count": counts[0], "total_bytes": counts[1]}
            for name, counts in sorted(totals.items())
        ]
        return owners, sum(row["total_bytes"] for row in owners)


@pytest.fixture()
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture()
def client(store: FakeStore):
    app_module.app.dependency_overrides[app_module.get_store] = lambda: store
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _upload(client, owner="alice", filename="notes.txt") -> Dict[str, Any]:
    response = client.post(
        "/files/upload-url",
        json={"owner": owner, "filename": filename, "content_type": "text/plain"},
    )
    assert response.status_code == 201
    return response.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["bucket"] == storage_module.bucket_name()
    assert body["table"] == storage_module.table_name()
    assert body["region"] == storage_module.aws_region()


def test_create_upload_url(client):
    body = _upload(client)
    assert body["file_id"] == "file-1"
    assert body["s3_key"] == "alice/file-1/notes.txt"
    assert body["upload_url"].startswith("https://")
    assert body["expires_in"] == 900


def test_create_upload_url_validation(client):
    response = client.post("/files/upload-url", json={"owner": "", "filename": "a.txt"})
    assert response.status_code == 422


def test_create_upload_url_blank_owner(client):
    response = client.post("/files/upload-url", json={"owner": "   ", "filename": "a.txt"})
    assert response.status_code == 422


def test_complete_upload(client):
    created = _upload(client)
    response = client.post("/files/{0}/complete".format(created["file_id"]))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["size_bytes"] == 2048


def test_complete_upload_missing(client):
    response = client.post("/files/does-not-exist/complete")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_list_files_with_pagination(client):
    for index in range(3):
        _upload(client, filename="f{0}.txt".format(index))
    _upload(client, owner="bob", filename="other.txt")

    first = client.get("/files", params={"owner": "alice", "limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert body["owner"] == "alice"
    assert body["count"] == 2
    assert body["next_token"] == "2"

    second = client.get("/files", params={"owner": "alice", "limit": 2, "next_token": body["next_token"]})
    assert second.status_code == 200
    assert second.json()["count"] == 1
    assert second.json()["next_token"] is None


def test_list_files_requires_owner(client):
    assert client.get("/files").status_code == 422


def test_list_files_invalid_token(client):
    response = client.get("/files", params={"owner": "alice", "next_token": "bad"})
    assert response.status_code == 400


def test_get_file_includes_download_url(client):
    created = _upload(client)
    response = client.get("/files/{0}".format(created["file_id"]))
    assert response.status_code == 200
    body = response.json()
    assert body["download_url"].endswith("download=1")
    assert body["filename"] == "notes.txt"


def test_get_file_missing(client):
    assert client.get("/files/nope").status_code == 404


def test_delete_file(client, store):
    created = _upload(client)
    response = client.delete("/files/{0}".format(created["file_id"]))
    assert response.status_code == 200
    assert response.json() == {
        "file_id": created["file_id"],
        "s3_key": created["s3_key"],
        "deleted": True,
    }
    assert store.items == {}


def test_delete_file_missing(client):
    assert client.delete("/files/nope").status_code == 404


def test_usage_for_owner_and_all(client):
    first = _upload(client, owner="alice", filename="a.txt")
    client.post("/files/{0}/complete".format(first["file_id"]))
    second = _upload(client, owner="bob", filename="b.txt")
    client.post("/files/{0}/complete".format(second["file_id"]))

    owner_usage = client.get("/usage", params={"owner": "alice"})
    assert owner_usage.status_code == 200
    assert owner_usage.json() == {
        "owners": [{"owner": "alice", "file_count": 1, "total_bytes": 2048}],
        "total_bytes": 2048,
    }

    all_usage = client.get("/usage")
    assert all_usage.status_code == 200
    body = all_usage.json()
    assert body["total_bytes"] == 4096
    assert [row["owner"] for row in body["owners"]] == ["alice", "bob"]


def test_usage_unknown_owner_is_zero(client):
    response = client.get("/usage", params={"owner": "ghost"})
    assert response.status_code == 200
    assert response.json()["owners"] == [{"owner": "ghost", "file_count": 0, "total_bytes": 0}]


def test_storage_error_maps_to_502(client, store):
    store.raise_storage_error = True
    response = client.get("/usage", params={"owner": "alice"})
    assert response.status_code == 502
    assert response.json()["detail"] == "dynamodb unavailable"


# --- storage layer tests (fakes injected, no boto3 calls) -------------------


@pytest.fixture()
def aws_store():
    s3 = FakeS3()
    table = FakeTable()
    return storage_module.DynamoS3FileStore(
        s3=s3, table=table, bucket="test-bucket", owner_index="owner-index", expires_in=120
    ), s3, table


def test_store_create_upload_url_writes_pending(aws_store):
    store, s3, table = aws_store
    result = store.create_upload_url("alice", "../evil/report.pdf", "application/pdf", 10)
    item = table.items[result["file_id"]]
    assert item["status"] == "pending"
    assert item["filename"] == "report.pdf"
    assert item["s3_key"].startswith("alice/")
    assert s3.presign_calls[0]["method"] == "put_object"
    assert s3.presign_calls[0]["expires_in"] == 120
    assert result["expires_in"] == 120


def test_store_complete_upload_uses_head_object(aws_store):
    store, s3, table = aws_store
    created = store.create_upload_url("alice", "data.bin")
    s3.sizes[created["s3_key"]] = 4321
    item = store.complete_upload(created["file_id"])
    assert item["size_bytes"] == 4321
    assert item["status"] == "available"
    assert table.items[created["file_id"]]["status"] == "available"


def test_store_complete_upload_missing_object(aws_store):
    store, _s3, _table = aws_store
    created = store.create_upload_url("alice", "data.bin")
    with pytest.raises(storage_module.NotFoundError):
        store.complete_upload(created["file_id"])


def test_store_get_missing_file(aws_store):
    store, _s3, _table = aws_store
    with pytest.raises(storage_module.NotFoundError):
        store.get_file("missing")


def test_store_delete_removes_object_and_item(aws_store):
    store, s3, table = aws_store
    created = store.create_upload_url("alice", "data.bin")
    item = store.delete_file(created["file_id"])
    assert item["s3_key"] == created["s3_key"]
    assert s3.deleted == [created["s3_key"]]
    assert table.deleted == [created["file_id"]]


def test_store_delete_tolerates_absent_object(aws_store):
    store, s3, table = aws_store
    created = store.create_upload_url("alice", "data.bin")
    s3.delete_error = FakeClientError("NoSuchKey")
    store.delete_file(created["file_id"])
    assert table.deleted == [created["file_id"]]


def test_store_delete_propagates_real_errors(aws_store):
    store, s3, _table = aws_store
    created = store.create_upload_url("alice", "data.bin")
    s3.delete_error = FakeClientError("AccessDenied")
    with pytest.raises(storage_module.StorageError):
        store.delete_file(created["file_id"])


def test_store_list_files_returns_token(aws_store):
    store, _s3, table = aws_store
    table.query_responses.append(
        {
            "Items": [{"file_id": "a", "owner": "alice", "s3_key": "alice/a/x", "size_bytes": 5}],
            "LastEvaluatedKey": {"file_id": "a"},
        }
    )
    items, token = store.list_files("alice", limit=1)
    assert items[0]["file_id"] == "a"
    assert token is not None
    assert storage_module.decode_token(token) == {"file_id": "a"}

    table.query_responses.append({"Items": []})
    _items, next_token = store.list_files("alice", limit=1, next_token=token)
    assert next_token is None
    assert table.query_calls[-1]["ExclusiveStartKey"] == {"file_id": "a"}


def test_store_usage_single_owner_paginates(aws_store):
    store, _s3, table = aws_store
    table.query_responses.append(
        {"Items": [{"owner": "alice", "size_bytes": 100}], "LastEvaluatedKey": {"file_id": "a"}}
    )
    table.query_responses.append({"Items": [{"owner": "alice", "size_bytes": 50}]})
    owners, total = store.usage("alice")
    assert owners == [{"owner": "alice", "file_count": 2, "total_bytes": 150}]
    assert total == 150


def test_store_usage_all_owners_scan(aws_store):
    store, _s3, table = aws_store
    table.scan_responses.append(
        {
            "Items": [
                {"owner": "alice", "size_bytes": 10},
                {"owner": "bob", "size_bytes": 30},
            ]
        }
    )
    owners, total = store.usage()
    assert owners == [
        {"owner": "alice", "file_count": 1, "total_bytes": 10},
        {"owner": "bob", "file_count": 1, "total_bytes": 30},
    ]
    assert total == 40


def test_store_download_url(aws_store):
    store, s3, _table = aws_store
    url = store.download_url("alice/1/a.txt")
    assert "get_object" in url
    assert s3.presign_calls[-1]["params"]["Bucket"] == "test-bucket"


def test_token_roundtrip_and_invalid():
    token = storage_module.encode_token({"file_id": "abc"})
    assert storage_module.decode_token(token) == {"file_id": "abc"}
    with pytest.raises(storage_module.InvalidTokenError):
        storage_module.decode_token("!!!not-a-token!!!")


def test_env_configuration(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("S3_BUCKET", "custom-bucket")
    monkeypatch.setenv("DYNAMODB_TABLE", "custom-table")
    monkeypatch.setenv("PRESIGN_EXPIRES_IN", "60")
    assert storage_module.aws_endpoint_url() == "http://localhost:4566"
    assert storage_module.bucket_name() == "custom-bucket"
    assert storage_module.table_name() == "custom-table"
    assert storage_module.presign_expires_in() == 60
    monkeypatch.setenv("PRESIGN_EXPIRES_IN", "not-a-number")
    assert storage_module.presign_expires_in() == storage_module.DEFAULT_EXPIRES_IN


def test_s3_client_uses_endpoint(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    client = storage_module.s3_client()
    assert client.meta.endpoint_url == "http://localhost:4566"
    assert client.meta.region_name == "us-east-1"
