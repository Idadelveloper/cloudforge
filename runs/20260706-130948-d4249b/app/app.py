import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from storage import NoteRepository, DynamoDBNoteRepository


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NoteCreate(BaseModel):
    title: str = Field(..., min_length=1)
    body: str = Field(default="")


class NoteUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1)
    body: Optional[str] = None


class Note(BaseModel):
    note_id: str
    title: str
    body: str
    created_at: str
    updated_at: str


_repository: Optional[NoteRepository] = None


def get_repository() -> NoteRepository:
    global _repository
    if _repository is None:
        _repository = DynamoDBNoteRepository()
    return _repository


def set_repository(repo: Optional[NoteRepository]) -> None:
    global _repository
    _repository = repo


app = FastAPI(title="personal_notes_api")


@app.post("/notes", response_model=Note, status_code=201)
def create_note(payload: NoteCreate, repo: NoteRepository = Depends(get_repository)):
    now = _now_iso()
    note = {
        "note_id": str(uuid.uuid4()),
        "title": payload.title,
        "body": payload.body,
        "created_at": now,
        "updated_at": now,
    }
    repo.put(note)
    return note


@app.get("/notes", response_model=List[Note])
def list_notes(repo: NoteRepository = Depends(get_repository)):
    return repo.list()


@app.get("/notes/{note_id}", response_model=Note)
def get_note(note_id: str, repo: NoteRepository = Depends(get_repository)):
    note = repo.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.put("/notes/{note_id}", response_model=Note)
def update_note(
    note_id: str,
    payload: NoteUpdate,
    repo: NoteRepository = Depends(get_repository),
):
    note = repo.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    if payload.title is not None:
        note["title"] = payload.title
    if payload.body is not None:
        note["body"] = payload.body
    note["updated_at"] = _now_iso()
    repo.put(note)
    return note


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: str, repo: NoteRepository = Depends(get_repository)):
    note = repo.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    repo.delete(note_id)
    return None


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
