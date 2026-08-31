import os
from typing import Dict, List, Optional

import boto3


def dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


class NotesRepository:
    """Data access layer for notes backed by DynamoDB."""

    def __init__(self, table_name: Optional[str] = None, table=None):
        self.table_name = table_name or os.environ.get("NOTES_TABLE", "notes")
        self._table = table

    @property
    def table(self):
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    def put(self, item: Dict) -> None:
        self.table.put_item(Item=item)

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
