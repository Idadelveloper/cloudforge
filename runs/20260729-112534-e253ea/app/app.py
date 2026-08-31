import secrets
import string
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from storage import DynamoStorage, StorageRepository

CODE_ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 7
MAX_COLLISION_RETRIES = 5

app = FastAPI(title="url_shortener")

_storage_singleton = None


def get_storage() -> StorageRepository:
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = DynamoStorage()
    return _storage_singleton


class ShortenRequest(BaseModel):
    url: str


def _is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


@app.post("/shorten")
def shorten(payload: ShortenRequest, storage: StorageRepository = Depends(get_storage)):
    if not _is_valid_url(payload.url):
        raise HTTPException(status_code=400, detail="Invalid URL")

    created_at = datetime.now(timezone.utc).isoformat()
    for _ in range(MAX_COLLISION_RETRIES):
        code = _generate_code()
        if storage.create_mapping(code, payload.url, created_at):
            return {"code": code, "short_url": f"/{code}", "long_url": payload.url}
    raise HTTPException(status_code=500, detail="Could not generate unique code")


@app.get("/stats/{code}")
def stats(code: str, storage: StorageRepository = Depends(get_storage)):
    item = storage.get_mapping(code)
    if item is None:
        raise HTTPException(status_code=404, detail="Code not found")
    return {
        "code": code,
        "long_url": item["long_url"],
        "visit_count": int(item.get("visit_count", 0)),
        "created_at": item.get("created_at"),
    }


@app.get("/{code}")
def redirect(code: str, storage: StorageRepository = Depends(get_storage)):
    item = storage.increment_visit(code)
    if item is None:
        raise HTTPException(status_code=404, detail="Code not found")
    return RedirectResponse(url=item["long_url"], status_code=302)
