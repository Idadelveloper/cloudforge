import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module  # noqa: E402
from app import app, get_repository  # noqa: E402


class FakeRepository:
    def __init__(self):
        self.store = {}

    def put(self, item):
        self.store[item["note_id"]] = dict(item)

    def get(self, note_id):
        item = self.store.get(note_id)
        return dict(item) if item is not None else None

    def list(self):
        return [dict(v) for v in self.store.values()]

    def delete(self, note_id):
        self.store.pop(note_id, None)


fake_repo = FakeRepository()


def override_repo():
    return fake_repo


app.dependency_overrides[get_repository] = override_repo
client = TestClient(app)


def setup_function(_):
    fake_repo.store.clear()
    app_module._repository = None


def test_create_note():
    resp = client.post("/notes", json={"title": "Hello", "body": "World"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Hello"
    assert data["body"] == "World"
    assert data["note_id"]
    assert data["created_at"] == data["updated_at"]


def test_list_notes():
    client.post("/notes", json={"title": "A", "body": "1"})
    client.post("/notes", json={"title": "B", "body": "2"})
    resp = client.get("/notes")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_note():
    created = client.post("/notes", json={"title": "X", "body": "Y"}).json()
    note_id = created["note_id"]
    resp = client.get(f"/notes/{note_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "X"


def test_get_note_not_found():
    resp = client.get("/notes/missing")
    assert resp.status_code == 404


def test_update_note():
    created = client.post("/notes", json={"title": "Old", "body": "Body"}).json()
    note_id = created["note_id"]
    resp = client.put(f"/notes/{note_id}", json={"title": "New"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "New"
    assert data["body"] == "Body"


def test_update_note_not_found():
    resp = client.put("/notes/missing", json={"title": "New"})
    assert resp.status_code == 404


def test_delete_note():
    created = client.post("/notes", json={"title": "Del", "body": "Me"}).json()
    note_id = created["note_id"]
    resp = client.delete(f"/notes/{note_id}")
    assert resp.status_code == 204
    assert client.get(f"/notes/{note_id}").status_code == 404


def test_delete_note_not_found():
    resp = client.delete("/notes/missing")
    assert resp.status_code == 404
