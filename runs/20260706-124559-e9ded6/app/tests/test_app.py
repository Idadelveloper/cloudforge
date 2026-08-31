import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, get_repository  # noqa: E402


class FakeRepository:
    def __init__(self):
        self.items = {}

    def create(self, note):
        self.items[note["note_id"]] = note
        return note

    def list(self):
        return list(self.items.values())

    def get(self, note_id):
        return self.items.get(note_id)

    def update(self, note):
        self.items[note["note_id"]] = note
        return note

    def delete(self, note_id):
        self.items.pop(note_id, None)


fake_repo = FakeRepository()


def override_repository():
    return fake_repo


app.dependency_overrides[get_repository] = override_repository
client = TestClient(app)


def setup_function(_):
    fake_repo.items.clear()


def test_create_note():
    resp = client.post("/notes", json={"title": "T", "body": "B"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "T"
    assert data["body"] == "B"
    assert data["note_id"]
    assert data["created_at"] == data["updated_at"]


def test_list_notes():
    client.post("/notes", json={"title": "A", "body": "1"})
    client.post("/notes", json={"title": "B", "body": "2"})
    resp = client.get("/notes")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_note():
    created = client.post("/notes", json={"title": "T", "body": "B"}).json()
    note_id = created["note_id"]
    resp = client.get(f"/notes/{note_id}")
    assert resp.status_code == 200
    assert resp.json()["note_id"] == note_id


def test_get_note_not_found():
    resp = client.get("/notes/missing")
    assert resp.status_code == 404


def test_update_note():
    created = client.post("/notes", json={"title": "T", "body": "B"}).json()
    note_id = created["note_id"]
    resp = client.put(f"/notes/{note_id}", json={"title": "New"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "New"
    assert data["body"] == "B"


def test_update_note_not_found():
    resp = client.put("/notes/missing", json={"title": "X"})
    assert resp.status_code == 404


def test_delete_note():
    created = client.post("/notes", json={"title": "T", "body": "B"}).json()
    note_id = created["note_id"]
    resp = client.delete(f"/notes/{note_id}")
    assert resp.status_code == 204
    assert client.get(f"/notes/{note_id}").status_code == 404


def test_delete_note_not_found():
    resp = client.delete("/notes/missing")
    assert resp.status_code == 404
