"""Offline tests for the image gallery backend.

All AWS access is replaced either by an in-memory repository (HTTP layer
tests) or by hand-written S3/DynamoDB stubs (storage layer tests).
"""

import os
import sys

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import storage  # noqa: E402

NOW = "2024-01-01T00:00:00+00:00"


class FakeRepository:
    """In-memory stand-in for GalleryRepository."""

    def __init__(self):
        self.albums = {}
        self.images = {}
        self.objects = set()
        self._album_seq = 0
        self._image_seq = 0

    # health
    def health(self):
        return {
            "status": "ok",
            "checks": {"s3": "ok", "albums_table": "ok", "images_table": "ok"},
            "bucket": "test-bucket",
            "albums_table": "test-albums",
            "images_table": "test-images",
        }

    # albums
    def create_album(self, title, description=None):
        self._album_seq += 1
        album_id = "album-{}".format(self._album_seq)
        album = {
            "album_id": album_id,
            "title": title,
            "description": description,
            "image_count": 0,
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.albums[album_id] = album
        return dict(album)

    def list_albums(self, limit=50, next_token=None):
        storage.decode_token(next_token)
        items = sorted(self.albums.values(), key=lambda album: album["album_id"])
        return [dict(item) for item in items[:limit]], None

    def get_album(self, album_id):
        album = self.albums.get(album_id)
        if album is None:
            raise storage.NotFoundError("album '{}' not found".format(album_id))
        return dict(album)

    def update_album(self, album_id, title=None, description=None):
        album = self.albums.get(album_id)
        if album is None:
            raise storage.NotFoundError("album '{}' not found".format(album_id))
        if title is not None:
            album["title"] = title
        if description is not None:
            album["description"] = description
        album["updated_at"] = NOW
        return dict(album)

    def delete_album(self, album_id):
        self.get_album(album_id)
        keys = [key for key in list(self.images) if key[0] == album_id]
        objects = 0
        for key in keys:
            item = self.images.pop(key)
            if item["s3_key"] in self.objects:
                self.objects.discard(item["s3_key"])
                objects += 1
        self.albums.pop(album_id, None)
        return {
            "album_id": album_id,
            "deleted": True,
            "deleted_images": len(keys),
            "deleted_objects": objects,
        }

    # images
    def create_pending_image(self, album_id, filename, content_type="application/octet-stream", size_bytes=None):
        self.get_album(album_id)
        self._image_seq += 1
        image_id = "image-{}".format(self._image_seq)
        name = storage.safe_filename(filename)
        s3_key = "albums/{}/{}/{}".format(album_id, image_id, name)
        self.images[(album_id, image_id)] = {
            "album_id": album_id,
            "image_id": image_id,
            "filename": name,
            "content_type": content_type,
            "s3_key": s3_key,
            "size_bytes": size_bytes,
            "etag": None,
            "status": "pending",
            "created_at": NOW,
            "uploaded_at": None,
        }
        return {
            "image_id": image_id,
            "upload_url": "https://s3.test/{}?op=put_object".format(s3_key),
            "method": "PUT",
            "s3_key": s3_key,
            "expires_in": 900,
            "content_type": content_type,
        }

    def _image_item(self, album_id, image_id):
        item = self.images.get((album_id, image_id))
        if item is None:
            raise storage.NotFoundError("image '{}' not found".format(image_id))
        return item

    def _view(self, item):
        view = dict(item)
        view["download_url"] = "https://s3.test/{}?op=get_object".format(item["s3_key"])
        return view

    def complete_image(self, album_id, image_id):
        item = self._image_item(album_id, image_id)
        if item["s3_key"] not in self.objects:
            raise storage.ConflictError("object has not been uploaded yet")
        was_pending = item["status"] != "available"
        item.update(
            {
                "status": "available",
                "size_bytes": 1234,
                "etag": "abc123",
                "uploaded_at": NOW,
            }
        )
        if was_pending:
            self.albums[album_id]["image_count"] += 1
        return self._view(item)

    def list_images(self, album_id, limit=50, next_token=None):
        self.get_album(album_id)
        storage.decode_token(next_token)
        items = [self._view(item) for key, item in sorted(self.images.items()) if key[0] == album_id]
        return items[:limit], None

    def get_image(self, album_id, image_id):
        self.get_album(album_id)
        return self._view(self._image_item(album_id, image_id))

    def delete_image(self, album_id, image_id):
        item = self._image_item(album_id, image_id)
        self.objects.discard(item["s3_key"])
        del self.images[(album_id, image_id)]
        if item["status"] == "available":
            album = self.albums.get(album_id)
            if album and album["image_count"] > 0:
                album["image_count"] -= 1
        return {"album_id": album_id, "image_id": image_id, "deleted": True}


@pytest.fixture
def repo():
    return FakeRepository()


@pytest.fixture
def client(repo):
    app_module.app.dependency_overrides[app_module.get_repository] = lambda: repo
    with TestClient(app_module.app) as test_client:
        yield test_client
    app_module.app.dependency_overrides.clear()


def _create_album(client, title="Holidays", description="summer 2024"):
    response = client.post("/albums", json={"title": title, "description": description})
    assert response.status_code == 201, response.text
    return response.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["s3"] == "ok"


def test_create_and_get_album(client):
    album = _create_album(client)
    assert album["album_id"]
    assert album["title"] == "Holidays"
    assert album["image_count"] == 0

    fetched = client.get("/albums/{}".format(album["album_id"]))
    assert fetched.status_code == 200
    assert fetched.json()["description"] == "summer 2024"


def test_create_album_validation(client):
    response = client.post("/albums", json={"title": ""})
    assert response.status_code == 422


def test_list_albums(client):
    _create_album(client, "A")
    _create_album(client, "B")
    response = client.get("/albums", params={"limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["next_token"] is None
    assert {item["title"] for item in body["items"]} == {"A", "B"}


def test_list_albums_bad_token_and_limit(client):
    bad_token = client.get("/albums", params={"next_token": "!!!not-base64!!!"})
    assert bad_token.status_code == 400
    assert bad_token.json()["error"] == "bad_request"

    bad_limit = client.get("/albums", params={"limit": 0})
    assert bad_limit.status_code == 422


def test_get_missing_album(client):
    response = client.get("/albums/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_patch_album(client):
    album = _create_album(client)
    response = client.patch("/albums/{}".format(album["album_id"]), json={"title": "Renamed"})
    assert response.status_code == 200
    assert response.json()["title"] == "Renamed"

    empty = client.patch("/albums/{}".format(album["album_id"]), json={})
    assert empty.status_code == 400

    missing = client.patch("/albums/nope", json={"title": "x"})
    assert missing.status_code == 404


def test_image_upload_lifecycle(client, repo):
    album = _create_album(client)
    album_id = album["album_id"]

    presign = client.post(
        "/albums/{}/images".format(album_id),
        json={"filename": "my photo.png", "content_type": "image/png", "size_bytes": 10},
    )
    assert presign.status_code == 201
    upload = presign.json()
    assert upload["method"] == "PUT"
    assert upload["s3_key"].startswith("albums/{}/".format(album_id))
    assert upload["expires_in"] == 900

    image_id = upload["image_id"]
    too_early = client.post("/albums/{}/images/{}/complete".format(album_id, image_id))
    assert too_early.status_code == 409
    assert too_early.json()["error"] == "conflict"

    # simulate the browser PUT to the presigned URL
    repo.objects.add(upload["s3_key"])

    completed = client.post("/albums/{}/images/{}/complete".format(album_id, image_id))
    assert completed.status_code == 200
    body = completed.json()
    assert body["status"] == "available"
    assert body["size_bytes"] == 1234
    assert body["download_url"].endswith("op=get_object")

    listed = client.get("/albums/{}/images".format(album_id))
    assert listed.status_code == 200
    listing = listed.json()
    assert listing["count"] == 1
    assert listing["items"][0]["image_id"] == image_id
    assert listing["items"][0]["download_url"]

    single = client.get("/albums/{}/images/{}".format(album_id, image_id))
    assert single.status_code == 200
    assert single.json()["filename"] == "my_photo.png"

    assert client.get("/albums/{}".format(album_id)).json()["image_count"] == 1

    deleted = client.delete("/albums/{}/images/{}".format(album_id, image_id))
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get("/albums/{}".format(album_id)).json()["image_count"] == 0
    assert client.get("/albums/{}/images".format(album_id)).json()["count"] == 0


def test_image_endpoints_missing_resources(client):
    missing_album = client.post(
        "/albums/nope/images",
        json={"filename": "a.png", "content_type": "image/png"},
    )
    assert missing_album.status_code == 404

    album = _create_album(client)
    album_id = album["album_id"]
    assert client.get("/albums/{}/images/ghost".format(album_id)).status_code == 404
    assert client.delete("/albums/{}/images/ghost".format(album_id)).status_code == 404
    assert client.post("/albums/{}/images/ghost/complete".format(album_id)).status_code == 404


def test_delete_album_cascades(client, repo):
    album = _create_album(client)
    album_id = album["album_id"]
    upload = client.post(
        "/albums/{}/images".format(album_id),
        json={"filename": "pic.jpg", "content_type": "image/jpeg"},
    ).json()
    repo.objects.add(upload["s3_key"])
    client.post("/albums/{}/images/{}/complete".format(album_id, upload["image_id"]))

    response = client.delete("/albums/{}".format(album_id))
    assert response.status_code == 200
    body = response.json()
    assert body["deleted_images"] == 1
    assert body["deleted_objects"] == 1
    assert repo.objects == set()
    assert client.get("/albums/{}".format(album_id)).status_code == 404
    assert client.delete("/albums/{}".format(album_id)).status_code == 404


# --------------------------------------------------------------------------- #
# storage layer tests with stubbed boto3 clients
# --------------------------------------------------------------------------- #
class FakeS3:
    """Minimal stub of the boto3 S3 client surface used by the repository."""

    def __init__(self):
        self.objects = {}
        self.deleted = []

    def head_bucket(self, Bucket):
        return {}

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=900):
        params = Params or {}
        return "https://s3.test/{}/{}?op={}&ttl={}".format(
            params.get("Bucket"), params.get("Key"), operation, ExpiresIn
        )

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return self.objects[Key]

    def delete_objects(self, Bucket, Delete):
        for obj in Delete.get("Objects", []):
            self.deleted.append(obj["Key"])
            self.objects.pop(obj["Key"], None)
        return {"Deleted": Delete.get("Objects", [])}


class FakeTable:
    """Minimal stub of a boto3 DynamoDB Table resource."""

    def __init__(self, key_names):
        self.key_names = key_names
        self.items = {}
        self.updates = []

    def _key(self, data):
        return tuple(data[name] for name in self.key_names)

    def load(self):
        return None

    def put_item(self, Item, **kwargs):
        self.items[self._key(Item)] = dict(Item)
        return {}

    def get_item(self, Key, **kwargs):
        item = self.items.get(self._key(Key))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key, **kwargs):
        self.items.pop(self._key(Key), None)
        return {}

    def update_item(self, Key, **kwargs):
        self.updates.append((self._key(Key), kwargs))
        return {}


class FakeDynamo:
    """Stub DynamoDB resource returning pre-built fake tables."""

    def __init__(self, tables):
        self.tables = tables

    def Table(self, name):
        return self.tables[name]


@pytest.fixture
def storage_repo():
    settings = storage.Settings(
        bucket="test-bucket",
        albums_table="test-albums",
        images_table="test-images",
        presign_ttl=60,
    )
    s3 = FakeS3()
    albums = FakeTable(["album_id"])
    images = FakeTable(["album_id", "image_id"])
    dynamo = FakeDynamo({"test-albums": albums, "test-images": images})
    repository = storage.GalleryRepository(settings, s3=s3, dynamodb=dynamo)
    return repository, s3, albums, images


def test_storage_health(storage_repo):
    repository, _s3, _albums, _images = storage_repo
    result = repository.health()
    assert result["status"] == "ok"
    assert result["bucket"] == "test-bucket"


def test_storage_album_and_image_flow(storage_repo):
    repository, s3, albums, images = storage_repo

    album = repository.create_album("Trip", "desc")
    assert album["album_id"]
    assert len(albums.items) == 1
    assert repository.get_album(album["album_id"])["title"] == "Trip"

    with pytest.raises(storage.NotFoundError):
        repository.get_album("missing")

    pending = repository.create_pending_image(album["album_id"], "../my photo.png", "image/png", 10)
    assert pending["s3_key"].endswith("my_photo.png")
    assert "op=put_object" in pending["upload_url"]
    assert pending["expires_in"] == 60
    assert len(images.items) == 1

    with pytest.raises(storage.ConflictError):
        repository.complete_image(album["album_id"], pending["image_id"])

    s3.objects[pending["s3_key"]] = {
        "ContentLength": 10,
        "ETag": '"deadbeef"',
        "ContentType": "image/png",
    }
    completed = repository.complete_image(album["album_id"], pending["image_id"])
    assert completed["status"] == "available"
    assert completed["size_bytes"] == 10
    assert completed["etag"] == "deadbeef"
    assert "op=get_object" in completed["download_url"]
    assert albums.updates, "album counter should have been incremented"

    result = repository.delete_image(album["album_id"], pending["image_id"])
    assert result["deleted"] is True
    assert pending["s3_key"] in s3.deleted
    assert images.items == {}

    with pytest.raises(storage.NotFoundError):
        repository.get_image(album["album_id"], pending["image_id"])


def test_token_helpers():
    token = storage.encode_token({"album_id": "a1"})
    assert storage.decode_token(token) == {"album_id": "a1"}
    assert storage.encode_token(None) is None
    assert storage.decode_token(None) is None
    with pytest.raises(storage.BadRequestError):
        storage.decode_token("###")


def test_load_settings_defaults(monkeypatch):
    for name in ("GALLERY_BUCKET", "ALBUMS_TABLE", "IMAGES_TABLE", "PRESIGN_TTL_SECONDS", "APP_CONFIG_SECRET"):
        monkeypatch.delenv(name, raising=False)
    settings = storage.load_settings()
    assert settings.bucket == "image-gallery-media"
    assert settings.albums_table == "image-gallery-albums"
    assert settings.images_table == "image-gallery-images"
    assert settings.presign_ttl == 900


def test_load_settings_from_env(monkeypatch):
    monkeypatch.setenv("GALLERY_BUCKET", "my-bucket")
    monkeypatch.setenv("ALBUMS_TABLE", "my-albums")
    monkeypatch.setenv("IMAGES_TABLE", "my-images")
    monkeypatch.setenv("PRESIGN_TTL_SECONDS", "not-a-number")
    monkeypatch.delenv("APP_CONFIG_SECRET", raising=False)
    settings = storage.load_settings()
    assert settings.bucket == "my-bucket"
    assert settings.albums_table == "my-albums"
    assert settings.images_table == "my-images"
    assert settings.presign_ttl == 900
