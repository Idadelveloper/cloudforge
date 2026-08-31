import os
from typing import Dict, List, Optional, Protocol

import boto3


def dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


def table_name() -> str:
    return os.environ.get("NOTES_TABLE", "notes")


class NoteRepository(Protocol):
    def put(self, note: Dict) -> None:
        ...

    def get(self, note_id: str) -> Optional[Dict]:
        ...

    def list(self) -> List[Dict]:
        ...

    def delete(self, note_id: str) -> None:
        ...


class DynamoDBNoteRepository:
    """DynamoDB-backed implementation of the note repository."""

    def __init__(self, table=None):
        self._table = table

    @property
    def table(self):
        if self._table is None:
            self._table = dynamodb_resource().Table(table_name())
        return self._table

    def put(self, note: Dict) -> None:
        self.table.put_item(Item=note)

    def get(self, note_id: str) -> Optional[Dict]:
        response = self.table.get_item(Key={"note_id": note_id})
        return response.get("Item")

    def list(self) -> List[Dict]:
        items: List[Dict] = []
        response = self.table.scan()
        items.extend(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = self.table.scan(
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            items.extend(response.get("Items", []))
        return items

    def delete(self, note_id: str) -> None:
        self.table.delete_item(Key={"note_id": note_id})


class InMemoryNoteRepository:
    """Simple in-memory repository, useful for tests and local runs."""

    def __init__(self):
        self._store: Dict[str, Dict] = {}

    def put(self, note: Dict) -> None:
        self._store[note["note_id"]] = dict(note)

    def get(self, note_id: str) -> Optional[Dict]:
        item = self._store.get(note_id)
        return dict(item) if item is not None else None

    def list(self) -> List[Dict]:
        return [dict(item) for item in self._store.values()]

    def delete(self, note_id: str) -> None:
        self._store.pop(note_id, None)
