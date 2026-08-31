import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from app import app, get_storage  # noqa: E402
from storage import StorageRepository  # noqa: E402


class FakeStorage(StorageRepository):
    def __init__(self):
        self.items = {}

    def create_mapping(self, code, long_url, created_at):
        if code in self.items:
            return False
        self.items[code] = {
            "code": code,
            "long_url": long_url,
            "visit_count": 0,
            "created_at": created_at,
        }
        return True

    def get_mapping(self, code):
        return self.items.get(code)

    def increment_visit(self, code):
        item = self.items.get(code)
        if item is None:
            return None
        item["visit_count"] += 1
        return item


fake = FakeStorage()
app.dependency_overrides[get_storage] = lambda: fake
client = TestClient(app)


def setup_function(_):
    fake.items.clear()


def test_shorten_returns_code():
    resp = client.post("/shorten", json={"url": "https://example.com/page"})
    assert resp.status_code == 200
    data = resp.json()
    assert "code" in data
    assert data["long_url"] == "https://example.com/page"
    assert data["short_url"] == "/" + data["code"]


def test_shorten_invalid_url():
    resp = client.post("/shorten", json={"url": "not-a-url"})
    assert resp.status_code == 400


def test_redirect_and_increment():
    code = client.post("/shorten", json={"url": "https://example.org"}).json()["code"]
    resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.org"

    stats = client.get(f"/stats/{code}").json()
    assert stats["visit_count"] == 1

    client.get(f"/{code}", follow_redirects=False)
    stats = client.get(f"/stats/{code}").json()
    assert stats["visit_count"] == 2


def test_redirect_unknown_code():
    resp = client.get("/doesnotexist", follow_redirects=False)
    assert resp.status_code == 404


def test_stats_unknown_code():
    resp = client.get("/stats/doesnotexist")
    assert resp.status_code == 404


def test_stats_fields():
    code = client.post("/shorten", json={"url": "https://a.example"}).json()["code"]
    stats = client.get(f"/stats/{code}").json()
    assert stats["code"] == code
    assert stats["long_url"] == "https://a.example"
    assert stats["visit_count"] == 0
    assert stats["created_at"]


def test_collision_retry(monkeypatch):
    codes = iter(["dup1234", "dup1234", "new5678"])
    monkeypatch.setattr(app_module, "_generate_code", lambda: next(codes))
    first = client.post("/shorten", json={"url": "https://one.example"}).json()
    assert first["code"] == "dup1234"
    second = client.post("/shorten", json={"url": "https://two.example"}).json()
    assert second["code"] == "new5678"
