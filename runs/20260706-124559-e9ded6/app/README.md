# personal_notes_api

A REST API for managing personal notes with full CRUD operations. Notes are
persisted in DynamoDB.

## Endpoints

- `POST /notes` — create a new note
- `GET /notes` — list all notes
- `GET /notes/{note_id}` — fetch a single note
- `PUT /notes/{note_id}` — update a note's title and/or body
- `DELETE /notes/{note_id}` — delete a note

## Configuration

The service reads the following environment variables:

- `AWS_ENDPOINT_URL` — override the AWS endpoint (e.g. LocalStack).
- `AWS_DEFAULT_REGION` — AWS region (defaults to `us-east-1`).
- `NOTES_TABLE` — DynamoDB table name (defaults to `notes_table`).
- `PORT` — HTTP port when run directly (defaults to `8000`).

## Running locally

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Or run the module directly:

```bash
python app.py
```

## Testing

```bash
pip install -r requirements.txt pytest
pytest
```

Tests use an in-memory fake repository injected via FastAPI dependency
overrides, so no AWS or network access is required.
