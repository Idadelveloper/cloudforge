import os
from typing import Optional, Protocol

import boto3


def dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


class NoteRepository(Protocol):
    def put(self, note: dict) -> None:
        ...

    def get(self, note_id: str) -> Optional[dict]:
        ...

    def list(self) -> list:
        ...

    def delete(self, note_id: str) -> None:
        ...


class DynamoDBNoteRepository:
    def __init__(self, table_name: Optional[str] = None):
        self._table_name = table_name or os.environ.get("NOTES_TABLE", "notes")
        self._resource = dynamodb_resource()

    @property
    def _table(self):
        return self._resource.Table(self._table_name)

    def put(self, note: dict) -> None:
        self._table.put_item(Item=note)

    def get(self, note_id: str) -> Optional[dict]:
        response = self._table.get_item(Key={"note_id": note_id})
        return response.get("Item")

    def list(self) -> list:
        items = []
        response = self._table.scan()
        items.extend(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = self._table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))
        return items

    def delete(self, note_id: str) -> None:
        self._table.delete_item(Key={"note_id": note_id})


class InMemoryNoteRepository:
    def __init__(self):
        self._store: dict = {}

    def put(self, note: dict) -> None:
        self._store[note["note_id"]] = dict(note)

    def get(self, note_id: str) -> Optional[dict]:
        item = self._store.get(note_id)
        return dict(item) if item else None

    def list(self) -> list:
        return [dict(item) for item in self._store.values()]

    def delete(self, note_id: str) -> None:
        self._store.pop(note_id, None)
