import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from storage import DynamoDBNoteRepository, NoteRepository


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NoteCreate(BaseModel):
    title: str = Field(..., min_length=1)
    body: str = Field(default="")


class NoteUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    body: str | None = None


class Note(BaseModel):
    note_id: str
    title: str
    body: str
    created_at: str
    updated_at: str


_repository: NoteRepository | None = None


def get_repository() -> NoteRepository:
    global _repository
    if _repository is None:
        _repository = DynamoDBNoteRepository()
    return _repository


def set_repository(repo: NoteRepository | None) -> None:
    global _repository
    _repository = repo


app = FastAPI(title="personal_notes_api")


@app.post("/notes", response_model=Note, status_code=201)
def create_note(payload: NoteCreate, repo: NoteRepository = Depends(get_repository)) -> Note:
    now = _now_iso()
    note = {
        "note_id": str(uuid.uuid4()),
        "title": payload.title,
        "body": payload.body,
        "created_at": now,
        "updated_at": now,
    }
    repo.put(note)
    return Note(**note)


@app.get("/notes", response_model=list[Note])
def list_notes(repo: NoteRepository = Depends(get_repository)) -> list[Note]:
    return [Note(**item) for item in repo.list()]


@app.get("/notes/{note_id}", response_model=Note)
def get_note(note_id: str, repo: NoteRepository = Depends(get_repository)) -> Note:
    item = repo.get(note_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return Note(**item)


@app.put("/notes/{note_id}", response_model=Note)
def update_note(
    note_id: str,
    payload: NoteUpdate,
    repo: NoteRepository = Depends(get_repository),
) -> Note:
    item = repo.get(note_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Note not found")
    if payload.title is not None:
        item["title"] = payload.title
    if payload.body is not None:
        item["body"] = payload.body
    item["updated_at"] = _now_iso()
    repo.put(item)
    return Note(**item)


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: str, repo: NoteRepository = Depends(get_repository)) -> None:
    item = repo.get(note_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Note not found")
    repo.delete(note_id)
    return None
