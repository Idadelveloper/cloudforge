# personal_notes_api

A REST API for managing personal notes (create, list, fetch, update, delete).
Built with FastAPI and backed by DynamoDB.

## Endpoints

- `POST /notes` — create a note
- `GET /notes` — list all notes
- `GET /notes/{note_id}` — fetch a note
- `PUT /notes/{note_id}` — update a note
- `DELETE /notes/{note_id}` — delete a note

## Configuration

Environment variables:

- `AWS_ENDPOINT_URL` — set to your LocalStack endpoint (e.g. `http://localhost:4566`).
- `AWS_DEFAULT_REGION` — defaults to `us-east-1`.
- `NOTES_TABLE` — DynamoDB table name, defaults to `notes`.

## Running locally

```bash
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

Or run directly:

```bash
python app.py
```

## Testing

```bash
pip install -r requirements.txt pytest
pytest
```

Tests use an in-memory repository and do not require AWS or network access.
