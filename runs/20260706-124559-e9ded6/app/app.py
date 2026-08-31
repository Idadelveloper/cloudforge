import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from storage import NotesRepository

app = FastAPI(title="personal_notes_api")


def _now():
    return datetime.now(timezone.utc).isoformat()


class NoteCreate(BaseModel):
    title: str
    body: str


class NoteUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None


class Note(BaseModel):
    note_id: str
    title: str
    body: str
    created_at: str
    updated_at: str


def get_repository() -> NotesRepository:
    return NotesRepository()


@app.post("/notes", response_model=Note, status_code=201)
def create_note(
    payload: NoteCreate,
    repo: NotesRepository = Depends(get_repository),
):
    now = _now()
    note = {
        "note_id": str(uuid.uuid4()),
        "title": payload.title,
        "body": payload.body,
        "created_at": now,
        "updated_at": now,
    }
    repo.create(note)
    return note


@app.get("/notes", response_model=List[Note])
def list_notes(repo: NotesRepository = Depends(get_repository)):
    return repo.list()


@app.get("/notes/{note_id}", response_model=Note)
def get_note(
    note_id: str,
    repo: NotesRepository = Depends(get_repository),
):
    note = repo.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.put("/notes/{note_id}", response_model=Note)
def update_note(
    note_id: str,
    payload: NoteUpdate,
    repo: NotesRepository = Depends(get_repository),
):
    note = repo.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    if payload.title is not None:
        note["title"] = payload.title
    if payload.body is not None:
        note["body"] = payload.body
    note["updated_at"] = _now()
    repo.update(note)
    return note


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(
    note_id: str,
    repo: NotesRepository = Depends(get_repository),
):
    note = repo.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    repo.delete(note_id)
    return None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",  # nosec B104
        port=int(os.environ.get("PORT", "8000")),
    )
