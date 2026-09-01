"""Data-access and messaging layer (DynamoDB, SQS, SNS) behind small interfaces.

All AWS clients honour AWS_ENDPOINT_URL (LocalStack) and default to us-east-1.
Resource names come from environment variables with defaults that match the
infrastructure plan.
"""
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

LOGGER = logging.getLogger("order_processing_service.storage")

DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE = "orders"
DEFAULT_INDEX = "customer_id-created_at-index"
DEFAULT_QUEUE = "order-fulfilment-queue"
MAX_LIMIT = 100


class StorageError(RuntimeError):
    """Raised when an AWS backend call fails."""


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def aws_region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def aws_endpoint_url() -> Optional[str]:
    return os.environ.get("AWS_ENDPOINT_URL") or None


def orders_table_name() -> str:
    return os.environ.get("ORDERS_TABLE_NAME") or DEFAULT_TABLE


def customer_index_name() -> str:
    return os.environ.get("ORDERS_CUSTOMER_INDEX") or DEFAULT_INDEX


def queue_name() -> str:
    return os.environ.get("ORDER_QUEUE_NAME") or DEFAULT_QUEUE


def queue_url_from_env() -> Optional[str]:
    return os.environ.get("ORDER_QUEUE_URL") or None


def topic_arn() -> Optional[str]:
    return os.environ.get("ORDER_STATUS_TOPIC_ARN") or None


def dynamodb_resource():
    """Return a DynamoDB resource configured for AWS or LocalStack."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def sqs_client():
    """Return an SQS client configured for AWS or LocalStack."""
    return boto3.client(
        "sqs",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def sns_client():
    """Return an SNS client configured for AWS or LocalStack."""
    return boto3.client(
        "sns",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def to_dynamo(value: Any) -> Any:
    """Recursively convert floats to Decimal so DynamoDB accepts the item."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: to_dynamo(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(item) for item in value]
    return value


def from_dynamo(value: Any) -> Any:
    """Recursively convert Decimal values back into JSON-friendly numbers."""
    if isinstance(value, Decimal):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    if isinstance(value, dict):
        return {key: from_dynamo(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [from_dynamo(item) for item in value]
    return value


class OrderRepository:
    """Interface for order persistence."""

    def create_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def list_orders_by_customer(
        self,
        customer_id: str,
        status: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def update_status(
        self,
        order_id: str,
        new_status: str,
        reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class EventPublisher:
    """Interface for outbound messaging."""

    def send_fulfilment_message(self, message: Dict[str, Any]) -> Optional[str]:
        raise NotImplementedError

    def publish_status_event(self, event: Dict[str, Any]) -> Optional[str]:
        raise NotImplementedError


class DynamoOrderRepository(OrderRepository):
    """DynamoDB-backed implementation of :class:`OrderRepository`."""

    def __init__(self, table: Any = None, index_name: Optional[str] = None) -> None:
        self._table_obj = table
        self._index_name = index_name

    def _table(self) -> Any:
        if self._table_obj is None:
            self._table_obj = dynamodb_resource().Table(orders_table_name())
        return self._table_obj

    def _index(self) -> str:
        return self._index_name or customer_index_name()

    def create_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self._table().put_item(
                Item=to_dynamo(order),
                ConditionExpression="attribute_not_exists(order_id)",
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("put_item failed: {0}".format(exc)) from exc
        return order

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self._table().get_item(Key={"order_id": order_id})
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("get_item failed: {0}".format(exc)) from exc
        item = response.get("Item")
        if item is None:
            return None
        return from_dynamo(item)

    def list_orders_by_customer(
        self,
        customer_id: str,
        status: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        capped = max(1, min(int(limit), MAX_LIMIT))
        kwargs: Dict[str, Any] = {
            "IndexName": self._index(),
            "KeyConditionExpression": Key("customer_id").eq(customer_id),
            "ScanIndexForward": False,
            "Limit": capped,
        }
        if status:
            kwargs["FilterExpression"] = Attr("status").eq(status)
        try:
            response = self._table().query(**kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("query failed: {0}".format(exc)) from exc
        items = [from_dynamo(item) for item in response.get("Items", [])]
        return items[:capped]

    def update_status(
        self,
        order_id: str,
        new_status: str,
        reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        expression = "SET #st = :st, updated_at = :updated"
        values: Dict[str, Any] = {":st": new_status, ":updated": utc_now()}
        if reason is not None:
            expression += ", last_status_reason = :reason"
            values[":reason"] = reason
        try:
            response = self._table().update_item(
                Key={"order_id": order_id},
                UpdateExpression=expression,
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(order_id)",
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return None
            raise StorageError("update_item failed: {0}".format(exc)) from exc
        except BotoCoreError as exc:
            raise StorageError("update_item failed: {0}".format(exc)) from exc
        return from_dynamo(response.get("Attributes", {}))


class AwsEventPublisher(EventPublisher):
    """SQS + SNS implementation of :class:`EventPublisher`."""

    def __init__(
        self,
        sqs: Any = None,
        sns: Any = None,
        queue_url: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> None:
        self._sqs = sqs
        self._sns = sns
        self._queue_url = queue_url
        self._topic = topic

    def _sqs_client(self) -> Any:
        if self._sqs is None:
            self._sqs = sqs_client()
        return self._sqs

    def _sns_client(self) -> Any:
        if self._sns is None:
            self._sns = sns_client()
        return self._sns

    def _resolve_queue_url(self) -> str:
        if self._queue_url:
            return self._queue_url
        from_env = queue_url_from_env()
        if from_env:
            self._queue_url = from_env
            return self._queue_url
        try:
            response = self._sqs_client().get_queue_url(QueueName=queue_name())
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("get_queue_url failed: {0}".format(exc)) from exc
        self._queue_url = response["QueueUrl"]
        return self._queue_url

    def _resolve_topic(self) -> Optional[str]:
        return self._topic or topic_arn()

    def send_fulfilment_message(self, message: Dict[str, Any]) -> Optional[str]:
        body = json.dumps(message, default=str)
        try:
            response = self._sqs_client().send_message(
                QueueUrl=self._resolve_queue_url(),
                MessageBody=body,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("send_message failed: {0}".format(exc)) from exc
        return response.get("MessageId")

    def publish_status_event(self, event: Dict[str, Any]) -> Optional[str]:
        target = self._resolve_topic()
        if not target:
            LOGGER.warning("ORDER_STATUS_TOPIC_ARN is not configured; skipping SNS publish")
            return None
        subject = "Order {0} is {1}".format(event.get("order_id"), event.get("new_status"))
        try:
            response = self._sns_client().publish(
                TopicArn=target,
                Subject=subject[:100],
                Message=json.dumps(event, default=str),
                MessageAttributes={
                    "new_status": {
                        "DataType": "String",
                        "StringValue": str(event.get("new_status", "UNKNOWN")),
                    }
                },
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("publish failed: {0}".format(exc)) from exc
        return response.get("MessageId")
