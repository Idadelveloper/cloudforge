import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module  # noqa: E402
from storage import InMemoryNoteRepository  # noqa: E402


def setup_function(_func):
    app_module.set_repository(InMemoryNoteRepository())


def teardown_function(_func):
    app_module.set_repository(None)


client = TestClient(app_module.app)


def test_create_note():
    resp = client.post("/notes", json={"title": "Hello", "body": "World"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Hello"
    assert data["body"] == "World"
    assert data["note_id"]
    assert data["created_at"] == data["updated_at"]


def test_list_notes():
    client.post("/notes", json={"title": "A", "body": "a"})
    client.post("/notes", json={"title": "B", "body": "b"})
    resp = client.get("/notes")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    titles = {item["title"] for item in items}
    assert titles == {"A", "B"}


def test_get_note():
    created = client.post("/notes", json={"title": "X", "body": "y"}).json()
    note_id = created["note_id"]
    resp = client.get(f"/notes/{note_id}")
    assert resp.status_code == 200
    assert resp.json()["note_id"] == note_id


def test_get_note_not_found():
    resp = client.get("/notes/missing")
    assert resp.status_code == 404


def test_update_note():
    created = client.post("/notes", json={"title": "Old", "body": "body"}).json()
    note_id = created["note_id"]
    resp = client.put(f"/notes/{note_id}", json={"title": "New"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "New"
    assert data["body"] == "body"


def test_update_note_not_found():
    resp = client.put("/notes/missing", json={"title": "New"})
    assert resp.status_code == 404


def test_delete_note():
    created = client.post("/notes", json={"title": "D", "body": "d"}).json()
    note_id = created["note_id"]
    resp = client.delete(f"/notes/{note_id}")
    assert resp.status_code == 204
    assert client.get(f"/notes/{note_id}").status_code == 404


def test_delete_note_not_found():
    resp = client.delete("/notes/missing")
    assert resp.status_code == 404
