"""Offline tests for the image gallery backend.

Every AWS call is replaced either by an in-memory fake repository (HTTP layer
tests) or by tiny boto3 stubs (storage layer tests); nothing touches the
network or LocalStack.
"""

import copy

import pytest
from fastapi.testclient import TestClient

import storage
from app import app, get_repository

FIXED_NOW = "2024-01-01T00:00:00+00:00"


class FakeRepository:
    """In-memory stand-in for DynamoS3GalleryRepository."""

    def __init__(self):
        self.albums = {}
        self.images = {}
        self.uploaded = set()
        self.healthy = True
        self.presign_expires = 900
        self._counter = 0

    def _next_id(self, prefix):
        self._counter += 1
        return "{0}-{1}".format(prefix, self._counter)

    def health(self):
        state = "ok" if self.healthy else "error"
        checks = {"s3": state, "albums_table": state, "images_table": state}
        return {"ok": self.healthy, "checks": checks}

    def create_album(self, title, description=""):
        album_id = self._next_id("album")
        album = {
            "album_id": album_id,
            "title": title,
            "description": description or "",
            "image_count": 0,
            "created_at": FIXED_NOW,
            "updated_at": FIXED_NOW,
        }
        self.albums[album_id] = album
        self.images[album_id] = {}
        return copy.deepcopy(album)

    def list_albums(self, limit=50, next_token=None):
        ordered = [self.albums[key] for key in sorted(self.albums)]
        start = int(next_token) if next_token else 0
        page = ordered[start:start + limit]
        following = start + limit
        token = str(following) if following < len(ordered) else None
        return {"albums": copy.deepcopy(page), "next_token": token}

    def get_album(self, album_id):
        album = self.albums.get(album_id)
        return copy.deepcopy(album) if album else None

    def update_album(self, album_id, updates):
        album = self.albums.get(album_id)
        if album is None:
            raise storage.AlbumNotFound(album_id)
        for field in ("title", "description"):
            if updates.get(field) is not None:
                album[field] = updates[field]
        album["updated_at"] = "2024-01-02T00:00:00+00:00"
        return copy.deepcopy(album)

    def delete_album(self, album_id):
        if album_id not in self.albums:
            raise storage.AlbumNotFound(album_id)
        for item in self.images.pop(album_id, {}).values():
            self.uploaded.discard(item["s3_key"])
        del self.albums[album_id]

    def create_image(self, album_id, filename, content_type):
        album = self.albums.get(album_id)
        if album is None:
            raise storage.AlbumNotFound(album_id)
        image_id = self._next_id("image")
        key = storage.build_s3_key(album_id, image_id, filename)
        item = {
            "album_id": album_id,
            "image_id": image_id,
            "filename": filename,
            "s3_key": key,
            "content_type": content_type,
            "size_bytes": 0,
            "status": storage.STATUS_PENDING,
            "created_at": FIXED_NOW,
            "uploaded_at": None,
        }
        self.images.setdefault(album_id, {})[image_id] = item
        album["image_count"] += 1
        return {
            "image": copy.deepcopy(item),
            "upload_url": "https://s3.example.test/{0}?signed=put".format(key),
            "expires_in": self.presign_expires,
        }

    def complete_image(self, album_id, image_id):
        item = self.images.get(album_id, {}).get(image_id)
        if item is None:
            raise storage.ImageNotFound(image_id)
        if item["s3_key"] not in self.uploaded:
            raise storage.ObjectNotUploaded(item["s3_key"])
        item["status"] = storage.STATUS_AVAILABLE
        item["size_bytes"] = 1024
        item["uploaded_at"] = "2024-01-01T01:00:00+00:00"
        return self._with_url(item)

    def list_images(self, album_id):
        if album_id not in self.albums:
            raise storage.AlbumNotFound(album_id)
        values = self.images.get(album_id, {}).values()
        ordered = sorted(values, key=lambda entry: entry["image_id"])
        return [self._with_url(item) for item in ordered]

    def get_image(self, album_id, image_id):
        item = self.images.get(album_id, {}).get(image_id)
        if item is None:
            raise storage.ImageNotFound(image_id)
        return self._with_url(item)

    def delete_image(self, album_id, image_id):
        item = self.images.get(album_id, {}).pop(image_id, None)
        if item is None:
            raise storage.ImageNotFound(image_id)
        self.uploaded.discard(item["s3_key"])
        album = self.albums.get(album_id)
        if album and album["image_count"] > 0:
            album["image_count"] -= 1

    def mark_uploaded(self, s3_key):
        self.uploaded.add(s3_key)

    def _with_url(self, item):
        out = copy.deepcopy(item)
        if out["status"] == storage.STATUS_AVAILABLE:
            out["download_url"] = "https://s3.example.test/{0}?signed=get".format(out["s3_key"])
        else:
            out["download_url"] = None
        return out


@pytest.fixture()
def repo():
    return FakeRepository()


@pytest.fixture()
def client(repo):
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _create_album(client, title="Holiday", description="Summer 2024"):
    response = client.post("/albums", json={"title": title, "description": description})
    assert response.status_code == 201
    return response.json()


def _register_image(client, album_id, filename="beach.jpg", content_type="image/jpeg"):
    response = client.post(
        "/albums/{0}/images".format(album_id),
        json={"filename": filename, "content_type": content_type},
    )
    assert response.status_code == 201
    return response.json()


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["s3"] == "ok"


def test_health_degraded(client, repo):
    repo.healthy = False
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_create_album(client):
    album = _create_album(client)
    assert album["title"] == "Holiday"
    assert album["description"] == "Summer 2024"
    assert album["image_count"] == 0
    assert album["album_id"]


def test_create_album_requires_title(client):
    response = client.post("/albums", json={"description": "no title"})
    assert response.status_code == 422
    response = client.post("/albums", json={"title": ""})
    assert response.status_code == 422


def test_list_albums_paginates(client):
    for index in range(3):
        _create_album(client, title="Album {0}".format(index))
    first = client.get("/albums", params={"limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert len(body["albums"]) == 2
    assert body["next_token"] == "2"
    second = client.get("/albums", params={"limit": 2, "next_token": body["next_token"]})
    assert second.status_code == 200
    assert len(second.json()["albums"]) == 1
    assert second.json()["next_token"] is None


def test_get_album(client):
    album = _create_album(client)
    response = client.get("/albums/{0}".format(album["album_id"]))
    assert response.status_code == 200
    assert response.json()["album_id"] == album["album_id"]


def test_get_album_missing(client):
    response = client.get("/albums/does-not-exist")
    assert response.status_code == 404


def test_patch_album(client):
    album = _create_album(client)
    response = client.patch(
        "/albums/{0}".format(album["album_id"]),
        json={"title": "Renamed"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Renamed"
    assert body["description"] == "Summer 2024"


def test_patch_album_without_fields(client):
    album = _create_album(client)
    response = client.patch("/albums/{0}".format(album["album_id"]), json={})
    assert response.status_code == 400


def test_patch_album_missing(client):
    response = client.patch("/albums/nope", json={"title": "x"})
    assert response.status_code == 404


def test_delete_album_cascades(client, repo):
    album = _create_album(client)
    upload = _register_image(client, album["album_id"])
    repo.mark_uploaded(upload["s3_key"])
    response = client.delete("/albums/{0}".format(album["album_id"]))
    assert response.status_code == 204
    assert repo.albums == {}
    assert repo.uploaded == set()
    assert client.get("/albums/{0}".format(album["album_id"])).status_code == 404


def test_delete_album_missing(client):
    assert client.delete("/albums/nope").status_code == 404


def test_register_image_returns_presigned_url(client):
    album = _create_album(client)
    upload = _register_image(client, album["album_id"])
    assert upload["upload_url"].startswith("https://s3.example.test/")
    assert upload["s3_key"].startswith("albums/{0}/".format(album["album_id"]))
    assert upload["expires_in"] == 900
    assert upload["required_headers"] == {"Content-Type": "image/jpeg"}
    assert upload["status"] == "pending"


def test_register_image_rejects_bad_filename(client):
    album = _create_album(client)
    response = client.post(
        "/albums/{0}/images".format(album["album_id"]),
        json={"filename": "../etc/passwd", "content_type": "image/jpeg"},
    )
    assert response.status_code == 400


def test_register_image_unknown_album(client):
    response = client.post(
        "/albums/nope/images",
        json={"filename": "a.jpg", "content_type": "image/jpeg"},
    )
    assert response.status_code == 404


def test_complete_image_flow(client, repo):
    album = _create_album(client)
    upload = _register_image(client, album["album_id"])
    repo.mark_uploaded(upload["s3_key"])
    path = "/albums/{0}/images/{1}/complete".format(album["album_id"], upload["image_id"])
    response = client.post(path)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["size_bytes"] == 1024
    assert body["download_url"]


def test_complete_image_without_upload_conflicts(client):
    album = _create_album(client)
    upload = _register_image(client, album["album_id"])
    path = "/albums/{0}/images/{1}/complete".format(album["album_id"], upload["image_id"])
    response = client.post(path)
    assert response.status_code == 409


def test_complete_image_missing(client):
    album = _create_album(client)
    path = "/albums/{0}/images/ghost/complete".format(album["album_id"])
    assert client.post(path).status_code == 404


def test_list_images(client, repo):
    album = _create_album(client)
    first = _register_image(client, album["album_id"], filename="one.jpg")
    _register_image(client, album["album_id"], filename="two.jpg")
    repo.mark_uploaded(first["s3_key"])
    client.post("/albums/{0}/images/{1}/complete".format(album["album_id"], first["image_id"]))
    response = client.get("/albums/{0}/images".format(album["album_id"]))
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    statuses = sorted(image["status"] for image in body["images"])
    assert statuses == ["available", "pending"]


def test_list_images_unknown_album(client):
    assert client.get("/albums/nope/images").status_code == 404


def test_get_image(client, repo):
    album = _create_album(client)
    upload = _register_image(client, album["album_id"])
    repo.mark_uploaded(upload["s3_key"])
    client.post("/albums/{0}/images/{1}/complete".format(album["album_id"], upload["image_id"]))
    response = client.get("/albums/{0}/images/{1}".format(album["album_id"], upload["image_id"]))
    assert response.status_code == 200
    body = response.json()
    assert body["image_id"] == upload["image_id"]
    assert body["download_url"].endswith("signed=get")


def test_get_image_missing(client):
    album = _create_album(client)
    response = client.get("/albums/{0}/images/ghost".format(album["album_id"]))
    assert response.status_code == 404


def test_delete_image(client, repo):
    album = _create_album(client)
    upload = _register_image(client, album["album_id"])
    response = client.delete("/albums/{0}/images/{1}".format(album["album_id"], upload["image_id"]))
    assert response.status_code == 204
    assert repo.albums[album["album_id"]]["image_count"] == 0
    assert client.get("/albums/{0}/images".format(album["album_id"])).json()["count"] == 0


def test_delete_image_missing(client):
    album = _create_album(client)
    response = client.delete("/albums/{0}/images/ghost".format(album["album_id"]))
    assert response.status_code == 404


# ----------------------------------------------------------------------
# storage layer unit tests (boto3 stubs, no network)
# ----------------------------------------------------------------------
class StubTable:
    def __init__(self, name):
        self.name = name
        self.items = {}
        self.updates = []

    @staticmethod
    def _key_of(key):
        return tuple(sorted((str(k), str(v)) for k, v in key.items()))

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        key = {field: item[field] for field in ("album_id", "image_id") if field in item}
        self.items[self._key_of(key)] = item
        return {}

    def get_item(self, **kwargs):
        item = self.items.get(self._key_of(kwargs["Key"]))
        return {"Item": item} if item is not None else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        return {"Attributes": {}}


class StubDynamo:
    def __init__(self):
        self.tables = {}

    def Table(self, name):  # noqa: N802 - mirrors the boto3 resource API
        return self.tables.setdefault(name, StubTable(name))


class StubS3:
    def __init__(self):
        self.presigned = []

    def generate_presigned_url(self, operation, **kwargs):
        params = kwargs.get("Params", {})
        self.presigned.append((operation, params, kwargs.get("ExpiresIn")))
        return "https://s3.example.test/{0}?op={1}".format(params.get("Key", ""), operation)


def _stub_repository():
    return storage.DynamoS3GalleryRepository(
        bucket="test-bucket",
        albums_table="test-albums",
        images_table="test-images",
        presign_expires=300,
        s3=StubS3(),
        dynamodb=StubDynamo(),
    )


def test_storage_create_album_and_image_with_stubs():
    repository = _stub_repository()
    album = repository.create_album("Holiday", "Summer")
    assert album["title"] == "Holiday"
    assert album["image_count"] == 0
    result = repository.create_image(album["album_id"], "beach.jpg", "image/jpeg")
    assert result["expires_in"] == 300
    assert result["image"]["status"] == storage.STATUS_PENDING
    assert result["upload_url"].startswith("https://s3.example.test/albums/")
    operation, params, expires = repository.s3.presigned[0]
    assert operation == "put_object"
    assert params["Bucket"] == "test-bucket"
    assert params["ContentType"] == "image/jpeg"
    assert expires == 300


def test_storage_create_image_unknown_album():
    repository = _stub_repository()
    with pytest.raises(storage.AlbumNotFound):
        repository.create_image("missing", "beach.jpg", "image/jpeg")


def test_storage_helpers():
    assert storage.build_s3_key("a1", "i1", "pic.png") == "albums/a1/i1/pic.png"
    assert storage.album_prefix("a1") == "albums/a1/"
    token = storage.encode_token({"album_id": "a1"})
    assert storage.decode_token(token) == {"album_id": "a1"}
    assert storage.encode_token(None) is None
    assert storage.decode_token(None) is None
    assert storage.decode_token("!!!not-base64!!!") is None
    assert storage.to_int("7") == 7
    assert storage.to_int(None) == 0


def test_storage_error_code_detection():
    class FakeClientError(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

    assert storage.error_code(FakeClientError("404")) == "404"
    assert storage.is_not_found(FakeClientError("404")) is True
    assert storage.is_not_found(FakeClientError("ResourceNotFoundException")) is True
    assert storage.is_not_found(FakeClientError("AccessDenied")) is False
    assert storage.error_code(ValueError("boom")) == ""


def test_clients_use_endpoint_override(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    client_handle = storage.s3_client()
    assert client_handle.meta.endpoint_url == "http://localhost:4566"
    assert storage.aws_region() == "us-east-1"
    monkeypatch.delenv("AWS_ENDPOINT_URL")
    assert storage.aws_endpoint_url() is None


def test_repository_reads_environment_configuration(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "env-bucket")
    monkeypatch.setenv("ALBUMS_TABLE", "env-albums")
    monkeypatch.setenv("IMAGES_TABLE", "env-images")
    monkeypatch.setenv("PRESIGN_EXPIRES_SECONDS", "120")
    repository = storage.DynamoS3GalleryRepository()
    assert repository.bucket == "env-bucket"
    assert repository.albums_table_name == "env-albums"
    assert repository.images_table_name == "env-images"
    assert repository.presign_expires == 120
