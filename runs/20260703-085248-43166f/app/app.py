import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from storage import NotesRepository

app = FastAPI(title="personal_notes_api")

_repository: Optional[NotesRepository] = None


def get_repository() -> NotesRepository:
    global _repository
    if _repository is None:
        _repository = NotesRepository()
    return _repository


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.post("/notes", response_model=Note, status_code=201)
def create_note(payload: NoteCreate, repo: NotesRepository = Depends(get_repository)):
    now = _now_iso()
    item = {
        "note_id": str(uuid.uuid4()),
        "title": payload.title,
        "body": payload.body,
        "created_at": now,
        "updated_at": now,
    }
    repo.put(item)
    return item


@app.get("/notes", response_model=List[Note])
def list_notes(repo: NotesRepository = Depends(get_repository)):
    return repo.list()


@app.get("/notes/{note_id}", response_model=Note)
def get_note(note_id: str, repo: NotesRepository = Depends(get_repository)):
    item = repo.get(note_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return item


@app.put("/notes/{note_id}", response_model=Note)
def update_note(
    note_id: str,
    payload: NoteUpdate,
    repo: NotesRepository = Depends(get_repository),
):
    item = repo.get(note_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Note not found")
    if payload.title is not None:
        item["title"] = payload.title
    if payload.body is not None:
        item["body"] = payload.body
    item["updated_at"] = _now_iso()
    repo.put(item)
    return item


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: str, repo: NotesRepository = Depends(get_repository)):
    item = repo.get(note_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Note not found")
    repo.delete(note_id)
    return None
