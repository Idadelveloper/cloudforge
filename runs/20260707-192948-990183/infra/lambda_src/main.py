import json
import os
import uuid
from datetime import datetime, timezone

import boto3

TABLE_NAME = os.environ.get("NOTES_TABLE", "notes")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    method = event.get("httpMethod", "GET")
    path_params = event.get("pathParameters") or {}
    note_id = path_params.get("note_id")
    raw_body = event.get("body") or "{}"
    try:
        payload = json.loads(raw_body)
    except (ValueError, TypeError):
        payload = {}

    if method == "POST" and not note_id:
        new_id = str(uuid.uuid4())
        now = _now()
        item = {
            "note_id": new_id,
            "title": payload.get("title", ""),
            "body": payload.get("body", ""),
            "created_at": now,
            "updated_at": now,
        }
        table.put_item(Item=item)
        return _response(201, item)

    if method == "GET" and not note_id:
        result = table.scan()
        return _response(200, result.get("Items", []))

    if method == "GET" and note_id:
        result = table.get_item(Key={"note_id": note_id})
        item = result.get("Item")
        if not item:
            return _response(404, {"detail": "Note not found"})
        return _response(200, item)

    if method == "PUT" and note_id:
        result = table.get_item(Key={"note_id": note_id})
        item = result.get("Item")
        if not item:
            return _response(404, {"detail": "Note not found"})
        item["title"] = payload.get("title", item["title"])
        item["body"] = payload.get("body", item["body"])
        item["updated_at"] = _now()
        table.put_item(Item=item)
        return _response(200, item)

    if method == "DELETE" and note_id:
        table.delete_item(Key={"note_id": note_id})
        return _response(204, {})

    return _response(400, {"detail": "Unsupported operation"})
