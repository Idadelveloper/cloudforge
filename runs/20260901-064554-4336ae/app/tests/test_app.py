"""Offline tests for the document_store backend.

All AWS access is replaced either by the in-memory repository (HTTP level tests)
or by hand written boto3 stubs (data access layer tests). Nothing touches the
network or LocalStack.
"""

import base64
import io
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage  # noqa: E402
import uploads  # noqa: E402


@pytest.fixture()
def repo():
    return storage.InMemoryDocumentRepository()


@pytest.fixture()
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _upload(client, title="Design Doc", author="ada", tags="Alpha, beta",
            body=b"hello world", filename="doc.txt", content_type="text/plain"):
    files = {"file": (filename, io.BytesIO(body), content_type)}
    data = {"title": title, "author": author}
    if tags is not None:
        data["tags"] = tags
    return client.post("/documents", files=files, data=data)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["s3"] == "ok"
    assert body["region"]


def test_upload_document_and_fetch_metadata(client):
    response = _upload(client)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["version"] == 1
    assert created["tags"] == ["alpha", "beta"]
    assert created["size_bytes"] == len(b"hello world")
    assert created["checksum_md5"] == storage.md5_hex(b"hello world")
    assert created["s3_key"].startswith("documents/{0}/v1/".format(created["document_id"]))

    fetched = client.get("/documents/{0}".format(created["document_id"]))
    assert fetched.status_code == 200
    summary = fetched.json()
    assert summary["latest_version"] == 1
    assert summary["version_count"] == 1
    assert summary["title"] == "Design Doc"


def test_upload_with_repeated_tag_fields(client):
    files = {"file": ("multi.txt", io.BytesIO(b"payload"), "text/plain")}
    data = {"title": "Multi", "author": "bob", "tags": ["Alpha", "gamma,alpha"]}
    response = client.post("/documents", files=files, data=data)
    assert response.status_code == 201, response.text
    assert response.json()["tags"] == ["alpha", "gamma"]


def test_upload_json_base64(client):
    payload = {
        "title": "JSON Doc",
        "author": "grace",
        "tags": ["Gamma", "delta"],
        "filename": "notes.txt",
        "content_type": "text/plain",
        "content_base64": base64.b64encode(b"json body").decode("ascii"),
    }
    response = client.post("/documents", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["content_type"] == "text/plain"
    assert body["filename"] == "notes.txt"
    assert body["tags"] == ["delta", "gamma"]
    assert body["size_bytes"] == len(b"json body")


def test_upload_validation_errors(client):
    empty = client.post(
        "/documents",
        files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
        data={"title": "Empty", "author": "ada"},
    )
    assert empty.status_code == 400

    missing_title = client.post(
        "/documents",
        files={"file": ("a.txt", io.BytesIO(b"data"), "text/plain")},
        data={"author": "ada"},
    )
    assert missing_title.status_code == 400
    assert "title" in missing_title.json()["detail"]


def test_new_version_flow(client):
    created = _upload(client, body=b"v1").json()
    document_id = created["document_id"]

    response = client.post(
        "/documents/{0}/versions".format(document_id),
        files={"file": ("doc-v2.txt", io.BytesIO(b"second version"), "text/plain")},
        data={"tags": "revised"},
    )
    assert response.status_code == 201, response.text
    second = response.json()
    assert second["version"] == 2
    assert second["tags"] == ["revised"]
    assert second["title"] == created["title"]
    assert second["author"] == created["author"]

    listing = client.get("/documents/{0}/versions".format(document_id))
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 2
    assert [item["version"] for item in body["items"]] == [1, 2]

    summary = client.get("/documents/{0}".format(document_id)).json()
    assert summary["latest_version"] == 2
    assert summary["version_count"] == 2

    missing = client.post(
        "/documents/does-not-exist/versions",
        files={"file": ("x.txt", io.BytesIO(b"nope"), "text/plain")},
    )
    assert missing.status_code == 404


def test_list_documents_pagination(client):
    for index in range(3):
        assert _upload(client, title="Doc {0}".format(index)).status_code == 201
    first = client.get("/documents", params={"limit": 2}).json()
    assert first["total"] == 3
    assert first["count"] == 2
    assert first["limit"] == 2
    second = client.get("/documents", params={"limit": 2, "offset": 2}).json()
    assert second["count"] == 1
    assert client.get("/documents", params={"limit": 0}).status_code == 422


def test_search_by_tag(client):
    first = _upload(client, title="A", tags="alpha,shared").json()
    second = _upload(client, title="B", tags="shared").json()

    response = client.get("/documents/search", params={"tag": "SHARED"})
    assert response.status_code == 200
    body = response.json()
    assert body["tag"] == "shared"
    assert body["count"] == 2
    found = {item["document_id"] for item in body["items"]}
    assert found == {first["document_id"], second["document_id"]}
    assert body["items"][0]["latest_version"] == 1

    assert client.get("/documents/search", params={"tag": "nobody"}).json()["count"] == 0
    assert client.get("/documents/search").status_code == 422


def test_download_presigned_url(client):
    created = _upload(client).json()
    document_id = created["document_id"]

    response = client.get(
        "/documents/{0}/versions/1/download".format(document_id),
        params={"expires_in": 120},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["expires_in_seconds"] == 120
    assert body["version"] == 1
    assert body["url"].startswith("https://")
    assert body["expires_at"].endswith("Z")

    default = client.get("/documents/{0}/versions/1/download".format(document_id)).json()
    assert default["expires_in_seconds"] == storage.DEFAULT_PRESIGN_EXPIRY

    assert client.get("/documents/{0}/versions/9/download".format(document_id)).status_code == 404
    too_long = client.get(
        "/documents/{0}/versions/1/download".format(document_id),
        params={"expires_in": 99999},
    )
    assert too_long.status_code == 422


def test_delete_document(client):
    created = _upload(client, tags="alpha").json()
    document_id = created["document_id"]

    response = client.delete("/documents/{0}".format(document_id))
    assert response.status_code == 200
    assert response.json() == {"document_id": document_id, "deleted_versions": 1}

    assert client.get("/documents/{0}".format(document_id)).status_code == 404
    assert client.get("/documents/{0}/versions".format(document_id)).status_code == 404
    assert client.delete("/documents/{0}".format(document_id)).status_code == 404
    assert client.get("/documents/search", params={"tag": "alpha"}).json()["count"] == 0


def test_unknown_document_returns_404(client):
    assert client.get("/documents/unknown-id").status_code == 404


# --------------------------------------------------------------------------- #
# upload parser unit tests
# --------------------------------------------------------------------------- #
def test_parse_multipart_handles_binary_and_repeated_fields():
    boundary = b"----test-boundary"
    payload = b"\x00\xff\xfebinary\r\ndata"
    body = b"".join(
        [
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="file"; filename="report.pdf"\r\n',
            b"Content-Type: application/pdf\r\n\r\n",
            payload,
            b"\r\n",
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="tags"\r\n\r\nalpha\r\n',
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="tags"\r\n\r\nBeta, gamma\r\n',
            b"--" + boundary + b"--\r\n",
        ]
    )
    header = "multipart/form-data; boundary=" + boundary.decode("ascii")
    parsed = uploads.parse_upload(header, body, {})
    assert parsed.data == payload
    assert parsed.filename == "report.pdf"
    assert parsed.content_type == "application/pdf"
    assert parsed.values("tags") == ["alpha", "Beta, gamma"]
    assert storage.normalise_tags(parsed.values("tags")) == ["alpha", "beta", "gamma"]


def test_parse_upload_raw_body_uses_query_fields():
    query = {
        "title": ["Raw"],
        "author": ["bob"],
        "tags": ["alpha,beta"],
        "filename": ["raw.bin"],
    }
    parsed = uploads.parse_upload("application/octet-stream", b"\x01\x02raw", query)
    assert parsed.data == b"\x01\x02raw"
    assert parsed.filename == "raw.bin"
    assert parsed.field("title") == "Raw"
    assert storage.normalise_tags(parsed.values("tags")) == ["alpha", "beta"]


def test_parse_upload_json_errors():
    with pytest.raises(uploads.UploadError):
        uploads.parse_upload("application/json", b'{"title": "x"}', {})
    with pytest.raises(uploads.UploadError):
        uploads.parse_upload("application/json", b"not-json", {})
    with pytest.raises(uploads.UploadError):
        uploads.parse_upload("multipart/form-data", b"", {})


# --------------------------------------------------------------------------- #
# AWS data access layer with boto3 stubs
# --------------------------------------------------------------------------- #
class FakeS3:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def head_bucket(self, **kwargs):
        return {}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {"VersionId": "ver-{0}".format(len(self.objects))}

    def generate_presigned_url(self, operation, **kwargs):
        params = kwargs.get("Params") or {}
        return "https://s3.local/{0}?e={1}&v={2}".format(
            params.get("Key"), kwargs.get("ExpiresIn"), params.get("VersionId", "")
        )

    def delete_object(self, **kwargs):
        self.deleted.append((kwargs["Key"], kwargs.get("VersionId")))
        return {}


class FakeTable:
    def __init__(self, name):
        self.name = name
        self.items = []

    def load(self):
        return None

    def _key_fields(self):
        if "tag-index" in self.name:
            return ("tag", "document_id")
        return ("document_id", "version")

    def _identity(self, item):
        return tuple(item.get(field) for field in self._key_fields())

    def put_item(self, **kwargs):
        item = dict(kwargs["Item"])
        identity = self._identity(item)
        self.items = [entry for entry in self.items if self._identity(entry) != identity]
        self.items.append(item)
        return {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        for entry in self.items:
            if all(entry.get(name) == value for name, value in key.items()):
                return {"Item": entry}
        return {}

    def query(self, **kwargs):
        expression = kwargs["KeyConditionExpression"].get_expression()
        attribute = expression["values"][0].name
        wanted = expression["values"][1]
        matches = [entry for entry in self.items if entry.get(attribute) == wanted]
        limit = kwargs.get("Limit")
        if limit:
            matches = matches[: int(limit)]
        return {"Items": matches}

    def scan(self, **kwargs):
        return {"Items": list(self.items)}

    def delete_item(self, **kwargs):
        key = kwargs["Key"]
        self.items = [
            entry
            for entry in self.items
            if not all(entry.get(name) == value for name, value in key.items())
        ]
        return {}


class FakeDynamoDB:
    def __init__(self):
        self.tables = {}

    def Table(self, name):  # noqa: N802 - mirrors the boto3 resource API
        return self.tables.setdefault(name, FakeTable(name))


def test_aws_repository_round_trip():
    fake_s3 = FakeS3()
    fake_dynamodb = FakeDynamoDB()
    repo = storage.AwsDocumentRepository(
        s3=fake_s3,
        dynamodb=fake_dynamodb,
        bucket="document-store-documents",
        metadata_table="document-metadata",
        tag_table="document-tag-index",
    )

    health = repo.health()
    assert health["status"] == "ok"
    assert health["bucket"] == "document-store-documents"

    created = repo.create_document(
        title="Spec",
        author="ada",
        tags=["Alpha", "alpha"],
        filename="../spec file.txt",
        content_type="text/plain",
        data=b"first",
    )
    assert created["version"] == 1
    assert created["tags"] == ["alpha"]
    assert created["s3_version_id"] == "ver-1"
    assert " " not in created["filename"]
    assert created["s3_key"] in fake_s3.objects

    document_id = created["document_id"]
    second = repo.add_version(
        document_id,
        filename="spec.txt",
        content_type="text/plain",
        data=b"second",
        tags=["beta"],
    )
    assert second["version"] == 2
    assert second["title"] == "Spec"
    assert second["tags"] == ["beta"]

    versions = repo.list_versions(document_id)
    assert [item["version"] for item in versions] == [1, 2]
    assert repo.get_version(document_id, 2)["size_bytes"] == len(b"second")

    summary = repo.get_document(document_id)
    assert summary["latest_version"] == 2
    assert summary["version_count"] == 2

    items, total = repo.list_documents(limit=10, offset=0)
    assert total == 1
    assert items[0]["document_id"] == document_id

    presigned = repo.presigned_url(document_id, 2, 60)
    assert "e=60" in presigned["url"]
    assert presigned["s3_version_id"] == "ver-2"

    assert repo.search_by_tag("beta", limit=10)[0]["document_id"] == document_id
    assert repo.search_by_tag("alpha", limit=10) == []

    assert repo.delete_document(document_id) == 2
    assert len(fake_s3.deleted) == 2
    with pytest.raises(storage.DocumentNotFoundError):
        repo.get_document(document_id)
    with pytest.raises(storage.DocumentNotFoundError):
        repo.presigned_url(document_id, 1, 60)
    with pytest.raises(storage.DocumentNotFoundError):
        repo.add_version(document_id, filename="a.txt", content_type="text/plain", data=b"x")


def test_aws_clients_use_endpoint_environment(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    assert storage.aws_endpoint_url() == "http://localhost:4566"
    assert storage.aws_region() == "us-east-1"
    assert storage.s3_client().meta.endpoint_url == "http://localhost:4566"
    assert storage.dynamodb_resource().meta.client.meta.endpoint_url == "http://localhost:4566"


def test_resource_names_from_environment(monkeypatch):
    monkeypatch.setenv("DOCUMENTS_BUCKET", "custom-bucket")
    monkeypatch.setenv("METADATA_TABLE", "custom-metadata")
    monkeypatch.setenv("TAG_INDEX_TABLE", "custom-tags")
    assert storage.bucket_name() == "custom-bucket"
    assert storage.metadata_table_name() == "custom-metadata"
    assert storage.tag_table_name() == "custom-tags"
