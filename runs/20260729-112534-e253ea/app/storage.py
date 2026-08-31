import os

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("URL_MAPPINGS_TABLE", "url_mappings")


def dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
    )


class StorageRepository:
    """Interface for URL mapping persistence."""

    def create_mapping(self, code, long_url, created_at):
        raise NotImplementedError

    def get_mapping(self, code):
        raise NotImplementedError

    def increment_visit(self, code):
        raise NotImplementedError


class DynamoStorage(StorageRepository):
    def __init__(self, table_name=TABLE_NAME):
        self._table_name = table_name
        self._table = None

    @property
    def table(self):
        if self._table is None:
            self._table = dynamodb_resource().Table(self._table_name)
        return self._table

    def create_mapping(self, code, long_url, created_at):
        try:
            self.table.put_item(
                Item={
                    "code": code,
                    "long_url": long_url,
                    "visit_count": 0,
                    "created_at": created_at,
                },
                ConditionExpression="attribute_not_exists(code)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get_mapping(self, code):
        response = self.table.get_item(Key={"code": code})
        return response.get("Item")

    def increment_visit(self, code):
        try:
            response = self.table.update_item(
                Key={"code": code},
                UpdateExpression="ADD visit_count :one",
                ExpressionAttributeValues={":one": 1},
                ConditionExpression="attribute_exists(code)",
                ReturnValues="ALL_NEW",
            )
            return response.get("Attributes")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
