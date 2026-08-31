import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from storage import InMemoryNoteRepository  # noqa: E402


def make_client():
    app_module.set_repository(InMemoryNoteRepository())
    return TestClient(app_module.app)


def test_create_note():
    client = make_client()
    resp = client.post("/notes", json={"title": "Hello", "body": "World"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Hello"
    assert data["body"] == "World"
    assert data["note_id"]
    assert data["created_at"]
    assert data["updated_at"]


def test_list_notes():
    client = make_client()
    client.post("/notes", json={"title": "A", "body": "1"})
    client.post("/notes", json={"title": "B", "body": "2"})
    resp = client.get("/notes")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_note():
    client = make_client()
    created = client.post("/notes", json={"title": "X", "body": "Y"}).json()
    resp = client.get(f"/notes/{created['note_id']}")
    assert resp.status_code == 200
    assert resp.json()["note_id"] == created["note_id"]


def test_get_note_not_found():
    client = make_client()
    resp = client.get("/notes/missing")
    assert resp.status_code == 404


def test_update_note():
    client = make_client()
    created = client.post("/notes", json={"title": "Old", "body": "Body"}).json()
    resp = client.put(
        f"/notes/{created['note_id']}",
        json={"title": "New"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "New"
    assert data["body"] == "Body"


def test_update_note_not_found():
    client = make_client()
    resp = client.put("/notes/missing", json={"title": "New"})
    assert resp.status_code == 404


def test_delete_note():
    client = make_client()
    created = client.post("/notes", json={"title": "Del", "body": "Me"}).json()
    resp = client.delete(f"/notes/{created['note_id']}")
    assert resp.status_code == 204
    assert client.get(f"/notes/{created['note_id']}").status_code == 404


def test_delete_note_not_found():
    client = make_client()
    resp = client.delete("/notes/missing")
    assert resp.status_code == 404
