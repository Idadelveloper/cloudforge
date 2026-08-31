import os
import boto3


def _table_name():
    return os.environ.get("NOTES_TABLE", "notes_table")


def dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


class NotesRepository:
    """Data access layer for notes backed by DynamoDB."""

    def __init__(self, table=None):
        if table is None:
            table = dynamodb_resource().Table(_table_name())
        self._table = table

    def create(self, note):
        self._table.put_item(Item=note)
        return note

    def list(self):
        response = self._table.scan()
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = self._table.scan(
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            items.extend(response.get("Items", []))
        return items

    def get(self, note_id):
        response = self._table.get_item(Key={"note_id": note_id})
        return response.get("Item")

    def update(self, note):
        self._table.put_item(Item=note)
        return note

    def delete(self, note_id):
        self._table.delete_item(Key={"note_id": note_id})
