"""Offline tests for the document_store service."""

import base64
import os
import sys

os.environ.setdefault("DOCUMENT_STORE_API_KEY", "unit-test-key")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.pop("AWS_ENDPOINT_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import storage  # noqa: E402
import uploads  # noqa: E402

AUTH = {"X-API-Key": "unit-test-key"}


@pytest.fixture()
def repo():
    storage.reset_settings()
    return storage.InMemoryDocumentRepository()


@pytest.fixture()
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def json_upload(title="Design Doc", author="ada", tags="alpha,beta", content=b"hello world",
                filename="doc.txt", content_type="text/plain"):
    return {
        "title": title,
        "author": author,
        "tags": tags,
        "filename": filename,
        "content_type": content_type,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def multipart_body(fields, filename="doc.txt", content=b"multipart payload", boundary="testboundary"):
    chunks = []
    for name, value in fields.items():
        chunks.append(
            (
                "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, name, value)
            ).encode("utf-8")
        )
    header = (
        "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
        "Content-Type: text/plain\r\n\r\n" % (boundary, filename)
    ).encode("utf-8")
    chunks.append(header + content + b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(chunks)
    return body, "multipart/form-data; boundary=%s" % boundary


def create_document(client, **kwargs):
    response = client.post("/documents", json=json_upload(**kwargs), headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "document_store"
    assert payload["dependencies"]["s3"] == "in-memory"


def test_upload_requires_api_key(client):
    response = client.post("/documents", json=json_upload())
    assert response.status_code == 401
    bad = client.post("/documents", json=json_upload(), headers={"X-API-Key": "nope"})
    assert bad.status_code == 401


def test_upload_allowed_when_no_api_key_configured(client, monkeypatch):
    monkeypatch.setattr(storage.get_settings(), "api_key", None)
    response = client.post("/documents", json=json_upload(title="Open"))
    assert response.status_code == 201


def test_create_document_json(client):
    item = create_document(client, tags="Alpha, beta,alpha")
    assert item["version"] == 1
    assert item["title"] == "Design Doc"
    assert item["author"] == "ada"
    assert item["tags"] == ["alpha", "beta"]
    assert item["size_bytes"] == len(b"hello world")
    assert item["is_latest"] is True
    assert item["s3_key"].endswith(item["document_id"])
    assert len(item["checksum"]) == 64


def test_create_document_multipart(client):
    body, content_type = multipart_body({"title": "From Form", "author": "grace", "tags": "forms,upload"})
    headers = dict(AUTH)
    headers["Content-Type"] = content_type
    response = client.post("/documents", content=body, headers=headers)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["title"] == "From Form"
    assert item["author"] == "grace"
    assert item["tags"] == ["forms", "upload"]
    assert item["filename"] == "doc.txt"
    assert item["size_bytes"] == len(b"multipart payload")


def test_create_document_validation(client):
    missing_title = client.post("/documents", json=json_upload(title=" "), headers=AUTH)
    assert missing_title.status_code == 400
    empty_file = client.post("/documents", json=json_upload(content=b""), headers=AUTH)
    assert empty_file.status_code == 400
    bad_b64 = client.post(
        "/documents",
        json={"title": "t", "author": "a", "content_base64": "not base64!!"},
        headers=AUTH,
    )
    assert bad_b64.status_code == 400
    unsupported = client.post("/documents", content=b"raw", headers={**AUTH, "Content-Type": "text/plain"})
    assert unsupported.status_code == 415


def test_upload_too_large(client, monkeypatch):
    monkeypatch.setattr(storage.get_settings(), "max_upload_bytes", 4)
    response = client.post("/documents", json=json_upload(content=b"way too long"), headers=AUTH)
    assert response.status_code == 413


def test_list_documents_filter_and_pagination(client):
    create_document(client, title="One", author="ada")
    create_document(client, title="Two", author="ada")
    create_document(client, title="Three", author="grace")

    all_docs = client.get("/documents").json()
    assert all_docs["count"] == 3

    filtered = client.get("/documents", params={"author": "grace"}).json()
    assert filtered["count"] == 1
    assert filtered["items"][0]["author"] == "grace"

    page_one = client.get("/documents", params={"limit": 2}).json()
    assert page_one["count"] == 2
    assert page_one["next_token"]
    page_two = client.get(
        "/documents", params={"limit": 2, "next_token": page_one["next_token"]}
    ).json()
    assert page_two["count"] == 1
    assert page_two["next_token"] is None

    bad_token = client.get("/documents", params={"next_token": "!!!not-base64"})
    assert bad_token.status_code == 400


def test_version_lifecycle(client):
    item = create_document(client)
    document_id = item["document_id"]

    second = client.post(
        "/documents/%s/versions" % document_id,
        json=json_upload(title="Design Doc v2", content=b"second revision"),
        headers=AUTH,
    )
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2
    assert second.json()["title"] == "Design Doc v2"

    versions = client.get("/documents/%s/versions" % document_id).json()
    assert versions["count"] == 2
    assert [v["version"] for v in versions["versions"]] == [1, 2]
    assert versions["versions"][0]["is_latest"] is False
    assert versions["versions"][1]["is_latest"] is True
    assert versions["versions"][1]["s3_version_id"]

    single = client.get("/documents/%s/versions/1" % document_id).json()
    assert single["version"] == 1
    assert single["size_bytes"] == len(b"hello world")

    assert client.get("/documents/%s/versions/99" % document_id).status_code == 404
    assert client.get("/documents/unknown-id/versions").status_code == 404


def test_new_version_for_unknown_document(client):
    response = client.post("/documents/missing/versions", json=json_upload(), headers=AUTH)
    assert response.status_code == 404


def test_download_url(client):
    item = create_document(client)
    document_id = item["document_id"]

    default = client.get("/documents/%s/versions/1/download-url" % document_id).json()
    assert default["expires_in_seconds"] == storage.get_settings().default_expiry
    assert default["document_id"] == document_id
    assert default["version"] == 1
    assert default["url"].startswith("https://")
    assert default["expires_at"]

    custom = client.get(
        "/documents/%s/versions/1/download-url" % document_id, params={"expires_in": 60}
    ).json()
    assert custom["expires_in_seconds"] == 60

    too_long = client.get(
        "/documents/%s/versions/1/download-url" % document_id, params={"expires_in": 99999}
    )
    assert too_long.status_code == 422

    assert client.get("/documents/%s/versions/7/download-url" % document_id).status_code == 404


def test_search_by_tag(client):
    create_document(client, title="Tagged", tags="alpha,beta")
    create_document(client, title="Other", tags="gamma")

    hit = client.get("/search", params={"tag": "ALPHA"}).json()
    assert hit["tag"] == "alpha"
    assert hit["count"] == 1
    assert hit["items"][0]["title"] == "Tagged"

    secondary = client.get("/search", params={"tag": "beta"}).json()
    assert secondary["count"] == 1

    miss = client.get("/search", params={"tag": "nothing"}).json()
    assert miss["count"] == 0

    assert client.get("/search").status_code == 422


def test_delete_document(client):
    item = create_document(client)
    document_id = item["document_id"]
    client.post("/documents/%s/versions" % document_id, json=json_upload(), headers=AUTH)

    unauthorized = client.delete("/documents/%s" % document_id)
    assert unauthorized.status_code == 401

    deleted = client.delete("/documents/%s" % document_id, headers=AUTH)
    assert deleted.status_code == 200
    assert deleted.json()["deleted_versions"] == 2

    assert client.get("/documents/%s/versions" % document_id).status_code == 404
    assert client.delete("/documents/%s" % document_id, headers=AUTH).status_code == 404


def test_storage_helpers():
    assert storage.normalize_tags("A, b ,a") == ["a", "b"]
    assert storage.normalize_tags(["X", "y", "x"]) == ["x", "y"]
    assert storage.normalize_tags(None) == []
    assert storage.normalize_tags(7) == ["7"]

    token = storage.encode_token({"document_id": "abc", "version": 2})
    assert storage.decode_token(token) == {"document_id": "abc", "version": 2}
    assert storage.encode_token(None) is None
    assert storage.decode_token(None) is None
    with pytest.raises(ValueError):
        storage.decode_token("###")

    from decimal import Decimal

    cleaned = storage.to_jsonable({"a": Decimal("3"), "b": [Decimal("1.5")], "c": b"xy"})
    assert cleaned["a"] == 3
    assert cleaned["b"] == [1.5]
    assert isinstance(cleaned["c"], str)

    assert storage.object_key_for("id-1") == "documents/id-1"


def test_multipart_parser_errors():
    with pytest.raises(ValueError):
        uploads.parse_multipart_form(b"", "application/json")
    assert uploads.get_boundary("multipart/form-data; boundary=\"abc\"") == "abc"
    assert uploads.get_boundary("") is None
    body, content_type = multipart_body({"title": "t"})
    form = uploads.parse_multipart_form(body, content_type)
    assert form.fields["title"] == "t"
    assert form.files["file"].size == len(b"multipart payload")


def _eq_value(expression):
    if expression is None:
        return None
    built = expression.get_expression()
    values = built.get("values", ())
    return values[1] if len(values) > 1 else None


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.counter = 0
        self.deleted = []

    def head_bucket(self, **kwargs):
        return {}

    def put_object(self, **kwargs):
        self.counter += 1
        version_id = "v%d" % self.counter
        self.objects[(kwargs["Key"], version_id)] = kwargs["Body"]
        return {"VersionId": version_id}

    def generate_presigned_url(self, client_method, **kwargs):
        params = kwargs.get("Params", {})
        return "https://s3.test/%s/%s?%s&expires=%s" % (
            params.get("Bucket"),
            params.get("Key"),
            params.get("VersionId", "null"),
            kwargs.get("ExpiresIn"),
        )

    def list_object_versions(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        versions = [
            {"Key": key, "VersionId": version}
            for (key, version) in self.objects
            if key.startswith(prefix)
        ]
        return {"Versions": versions, "DeleteMarkers": []}

    def delete_object(self, **kwargs):
        self.deleted.append((kwargs["Key"], kwargs.get("VersionId")))
        self.objects.pop((kwargs["Key"], kwargs.get("VersionId")), None)
        return {}


class FakeTable:
    def __init__(self):
        self.items = []

    def load(self):
        return None

    def put_item(self, **kwargs):
        item = dict(kwargs["Item"])
        self.items = [
            existing
            for existing in self.items
            if not (
                existing["document_id"] == item["document_id"]
                and int(existing["version"]) == int(item["version"])
            )
        ]
        self.items.append(item)
        return {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        for item in self.items:
            if item["document_id"] == key["document_id"] and int(item["version"]) == int(key["version"]):
                return {"Item": dict(item)}
        return {}

    def update_item(self, **kwargs):
        key = kwargs["Key"]
        for item in self.items:
            if item["document_id"] == key["document_id"] and int(item["version"]) == int(key["version"]):
                item["is_latest"] = False
        return {}

    def delete_item(self, **kwargs):
        key = kwargs["Key"]
        self.items = [
            item
            for item in self.items
            if not (
                item["document_id"] == key["document_id"]
                and int(item["version"]) == int(key["version"])
            )
        ]
        return {}

    def query(self, **kwargs):
        value = _eq_value(kwargs.get("KeyConditionExpression"))
        index_name = str(kwargs.get("IndexName") or "")
        field = "document_id"
        if "tag" in index_name:
            field = "tag"
        elif "author" in index_name:
            field = "author"
        matches = [dict(item) for item in self.items if item.get(field) == value]
        matches.sort(key=lambda item: int(item["version"]), reverse=not kwargs.get("ScanIndexForward", True))
        limit = kwargs.get("Limit")
        if limit:
            matches = matches[:limit]
        return {"Items": matches}

    def scan(self, **kwargs):
        matches = [dict(item) for item in self.items]
        limit = kwargs.get("Limit")
        if limit:
            matches = matches[:limit]
        return {"Items": matches}


def test_aws_repository_with_fake_clients():
    storage.reset_settings()
    fake_s3 = FakeS3()
    fake_table = FakeTable()
    repository = storage.AwsDocumentRepository(
        settings=storage.get_settings(), s3=fake_s3, table=fake_table
    )

    health = repository.health()
    assert health["healthy"] is True
    assert health["dependencies"] == {"s3": "ok", "dynamodb": "ok"}

    first = repository.create_document(
        title="Spec",
        author="ada",
        tags="alpha,beta",
        filename="spec.txt",
        content_type="text/plain",
        data=b"content one",
    )
    document_id = first["document_id"]
    assert first["version"] == 1
    assert first["s3_version_id"] == "v1"

    second = repository.add_version(
        document_id=document_id,
        title=None,
        author=None,
        tags=None,
        filename="spec.txt",
        content_type="text/plain",
        data=b"content two",
    )
    assert second is not None
    assert second["version"] == 2
    assert second["tags"] == ["alpha", "beta"]

    versions = repository.list_versions(document_id)
    assert [item["version"] for item in versions] == [1, 2]
    assert versions[0]["is_latest"] is False

    assert repository.get_version(document_id, 2)["size_bytes"] == len(b"content two")
    assert repository.get_version(document_id, 5) is None

    presigned = repository.create_presigned_url(document_id, 2, 120)
    assert presigned is not None
    assert "v2" in presigned["url"]
    assert presigned["expires_in_seconds"] == 120
    assert repository.create_presigned_url(document_id, 9, 120) is None

    found = repository.search_by_tag("alpha", 10)
    assert len(found) == 2

    items, token = repository.list_documents(limit=10)
    assert len(items) == 2
    assert token is None

    by_author, _ = repository.list_documents(author="ada", limit=10)
    assert len(by_author) == 2

    assert repository.add_version(
        document_id="missing",
        title=None,
        author=None,
        tags=None,
        filename="x",
        content_type="text/plain",
        data=b"x",
    ) is None

    assert repository.delete_document(document_id) == 2
    assert fake_table.items == []
    assert len(fake_s3.deleted) == 2
    assert repository.delete_document(document_id) == 0
