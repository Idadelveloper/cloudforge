# personal_notes_api

A REST API for managing personal notes, backed by DynamoDB.

## Endpoints

- `POST /notes` — create a note
- `GET /notes` — list all notes
- `GET /notes/{note_id}` — fetch a note
- `PUT /notes/{note_id}` — update a note
- `DELETE /notes/{note_id}` — delete a note

## Configuration

Environment variables:

- `AWS_ENDPOINT_URL` — override AWS endpoint (e.g. LocalStack `http://localhost:4566`).
- `AWS_DEFAULT_REGION` — defaults to `us-east-1`.
- `NOTES_TABLE` — DynamoDB table name, defaults to `notes`.

## Running locally

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

The DynamoDB table must exist with `note_id` (String) as the partition key.

## Testing

```bash
pip install -r requirements.txt pytest
pytest
```

Tests use an in-memory repository and require no network or AWS access.
