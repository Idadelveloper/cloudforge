# personal_notes_api

A REST API for managing personal notes, backed by DynamoDB.

## Endpoints

- `POST /notes` — create a note (`title`, `body`).
- `GET /notes` — list all notes.
- `GET /notes/{note_id}` — fetch a single note.
- `PUT /notes/{note_id}` — update a note's title and/or body.
- `DELETE /notes/{note_id}` — delete a note.

## Configuration

Environment variables:

- `AWS_ENDPOINT_URL` — set to your LocalStack endpoint (e.g. `http://localhost:4566`) for local development. Leave unset to use real AWS.
- `AWS_DEFAULT_REGION` — defaults to `us-east-1`.
- `NOTES_TABLE` — DynamoDB table name, defaults to `notes`.

## Running

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Testing

```bash
pip install -r requirements.txt pytest
pytest
```

Tests use an in-memory fake repository and require no AWS or network access.
