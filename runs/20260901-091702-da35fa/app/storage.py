"""Data access and messaging layer for the order processing service.

The module exposes three small interfaces -- ``OrderRepository``,
``FulfillmentQueue`` and ``OrderNotifier`` -- with a DynamoDB/SQS/SNS backed
implementation and an in-memory implementation used by the test-suite (and
usable for local development without AWS).

Every boto3 client honours ``AWS_ENDPOINT_URL`` so the service can talk to
LocalStack, and defaults to the ``us-east-1`` region.
"""

import abc
import base64
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_TABLE_NAME = "orders"
DEFAULT_CUSTOMER_INDEX = "customer_id-created_at-index"
DEFAULT_QUEUE_NAME = "order-fulfillment-queue"
DEFAULT_TOPIC_NAME = "order-status-changed-topic"

ALLOWED_STATUSES = (
    "PENDING",
    "QUEUED",
    "PROCESSING",
    "FULFILLED",
    "CANCELLED",
    "FAILED",
)


class StorageError(RuntimeError):
    """Raised when an underlying AWS (or in-memory) operation fails."""


class NotFoundError(StorageError):
    """Raised when the requested order does not exist."""


class InvalidTokenError(StorageError):
    """Raised when a pagination token cannot be decoded."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_order_id() -> str:
    """Return a fresh uuid4 order identifier."""
    return str(uuid.uuid4())


def quantize_amount(value: Any) -> Decimal:
    """Convert *value* into a two decimal place ``Decimal``."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def to_dynamo(value: Any) -> Any:
    """Recursively convert floats to Decimal and drop ``None`` mapping values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, dict):
        return {key: to_dynamo(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(item) for item in value]
    return value


def from_dynamo(value: Any) -> Any:
    """Recursively convert Decimal values into JSON friendly numbers."""
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: from_dynamo(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [from_dynamo(item) for item in value]
    return value


def encode_token(key: Dict[str, Any]) -> str:
    """Base64 encode a DynamoDB ``LastEvaluatedKey`` (or offset marker)."""
    raw = json.dumps(from_dynamo(key), separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: str) -> Dict[str, Any]:
    """Decode a pagination token produced by :func:`encode_token`."""
    if not token:
        raise InvalidTokenError("next_token must not be empty")
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise InvalidTokenError("invalid next_token: %s" % exc) from exc
    if not isinstance(data, dict):
        raise InvalidTokenError("invalid next_token payload")
    return data


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _error_message(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        return "%s: %s" % (error.get("Code", "ClientError"), error.get("Message", ""))
    return str(exc)


# --------------------------------------------------------------------------- #
# configuration + boto3 factories
# --------------------------------------------------------------------------- #
def aws_region() -> str:
    """Return the configured AWS region (default ``us-east-1``)."""
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


def aws_endpoint_url() -> Optional[str]:
    """Return ``AWS_ENDPOINT_URL`` when set (LocalStack), otherwise ``None``."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource() -> Any:
    """Create a DynamoDB resource pointed at LocalStack when configured."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def sqs_client() -> Any:
    """Create an SQS client pointed at LocalStack when configured."""
    return boto3.client(
        "sqs",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def sns_client() -> Any:
    """Create an SNS client pointed at LocalStack when configured."""
    return boto3.client(
        "sns",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def orders_table_name() -> str:
    """Name of the DynamoDB orders table."""
    return os.environ.get("ORDERS_TABLE_NAME") or os.environ.get("ORDERS_TABLE") or DEFAULT_TABLE_NAME


def customer_index_name() -> str:
    """Name of the customer_id GSI on the orders table."""
    return os.environ.get("ORDERS_CUSTOMER_INDEX") or DEFAULT_CUSTOMER_INDEX


def fulfillment_queue_name() -> str:
    """Name of the SQS fulfilment queue."""
    return (
        os.environ.get("ORDER_QUEUE_NAME")
        or os.environ.get("FULFILLMENT_QUEUE_NAME")
        or DEFAULT_QUEUE_NAME
    )


def fulfillment_queue_url() -> Optional[str]:
    """Explicit SQS queue URL, when provided by the environment."""
    return os.environ.get("ORDER_QUEUE_URL") or os.environ.get("FULFILLMENT_QUEUE_URL") or None


def status_topic_name() -> str:
    """Name of the SNS order-status topic."""
    return os.environ.get("ORDER_STATUS_TOPIC_NAME") or DEFAULT_TOPIC_NAME


def status_topic_arn() -> Optional[str]:
    """Explicit SNS topic ARN, when provided by the environment."""
    return os.environ.get("ORDER_STATUS_TOPIC_ARN") or os.environ.get("SNS_TOPIC_ARN") or None


# --------------------------------------------------------------------------- #
# interfaces
# --------------------------------------------------------------------------- #
class OrderRepository(abc.ABC):
    """Persistence interface for order records."""

    @abc.abstractmethod
    def put_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Store a new order record."""

    @abc.abstractmethod
    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Return the order or ``None`` when it does not exist."""

    @abc.abstractmethod
    def update_status(
        self,
        order_id: str,
        new_status: str,
        reason: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an order status, returning the new record."""

    @abc.abstractmethod
    def list_by_customer(
        self,
        customer_id: str,
        limit: int = 50,
        next_token: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return a page of orders for a customer plus the next token."""

    @abc.abstractmethod
    def health(self) -> Dict[str, Any]:
        """Raise :class:`StorageError` when the backend is unreachable."""


class FulfillmentQueue(abc.ABC):
    """Interface for the asynchronous fulfilment hand-off."""

    @abc.abstractmethod
    def send_fulfillment(self, message: Dict[str, Any]) -> str:
        """Publish a fulfilment message and return its identifier."""

    @abc.abstractmethod
    def health(self) -> Dict[str, Any]:
        """Raise :class:`StorageError` when the queue is unreachable."""


class OrderNotifier(abc.ABC):
    """Interface for order-status-changed notifications."""

    @abc.abstractmethod
    def publish_status_changed(self, event: Dict[str, Any]) -> str:
        """Publish a status change event and return its identifier."""

    @abc.abstractmethod
    def health(self) -> Dict[str, Any]:
        """Raise :class:`StorageError` when the topic is unreachable."""


# --------------------------------------------------------------------------- #
# AWS implementations
# --------------------------------------------------------------------------- #
class DynamoOrderRepository(OrderRepository):
    """DynamoDB backed :class:`OrderRepository`."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        index_name: Optional[str] = None,
        resource: Any = None,
    ) -> None:
        self.table_name = table_name or orders_table_name()
        self.index_name = index_name or customer_index_name()
        self._resource = resource
        self._table = None

    def _table_ref(self) -> Any:
        if self._table is None:
            resource = self._resource or dynamodb_resource()
            self._table = resource.Table(self.table_name)
        return self._table

    def put_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        item = to_dynamo(order)
        try:
            self._table_ref().put_item(Item=item)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("dynamodb put_item failed: %s" % _error_message(exc)) from exc
        return from_dynamo(item)

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self._table_ref().get_item(Key={"order_id": order_id})
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("dynamodb get_item failed: %s" % _error_message(exc)) from exc
        item = response.get("Item")
        return from_dynamo(item) if item else None

    def update_status(
        self,
        order_id: str,
        new_status: str,
        reason: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        stamp = updated_at or utc_now_iso()
        expression = "SET #status = :status, #updated_at = :updated_at"
        names = {"#status": "status", "#updated_at": "updated_at"}
        values: Dict[str, Any] = {":status": new_status, ":updated_at": stamp}
        if reason:
            expression += ", #reason = :reason"
            names["#reason"] = "status_reason"
            values[":reason"] = reason
        try:
            response = self._table_ref().update_item(
                Key={"order_id": order_id},
                UpdateExpression=expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(order_id)",
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            code = ""
            if isinstance(exc.response, dict):
                code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise NotFoundError("order '%s' not found" % order_id) from exc
            raise StorageError("dynamodb update_item failed: %s" % _error_message(exc)) from exc
        except BotoCoreError as exc:
            raise StorageError("dynamodb update_item failed: %s" % exc) from exc
        return from_dynamo(response.get("Attributes") or {})

    def list_by_customer(
        self,
        customer_id: str,
        limit: int = 50,
        next_token: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {
            "IndexName": self.index_name,
            "KeyConditionExpression": Key("customer_id").eq(customer_id),
            "ScanIndexForward": False,
            "Limit": max(1, min(int(limit), 100)),
        }
        if status:
            kwargs["FilterExpression"] = Attr("status").eq(status)
        if next_token:
            kwargs["ExclusiveStartKey"] = decode_token(next_token)
        try:
            response = self._table_ref().query(**kwargs)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("dynamodb query failed: %s" % _error_message(exc)) from exc
        orders = [from_dynamo(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        return orders, (encode_token(last_key) if last_key else None)

    def health(self) -> Dict[str, Any]:
        try:
            self._table_ref().load()
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("dynamodb table '%s' unavailable: %s" % (self.table_name, _error_message(exc))) from exc
        return {"table": self.table_name, "index": self.index_name}


class SqsFulfillmentQueue(FulfillmentQueue):
    """SQS backed :class:`FulfillmentQueue`."""

    def __init__(
        self,
        queue_url: Optional[str] = None,
        queue_name: Optional[str] = None,
        client: Any = None,
    ) -> None:
        self._queue_url = queue_url or fulfillment_queue_url()
        self.queue_name = queue_name or fulfillment_queue_name()
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            self._client = sqs_client()
        return self._client

    def queue_url(self) -> str:
        if not self._queue_url:
            try:
                response = self.client().get_queue_url(QueueName=self.queue_name)
            except (ClientError, BotoCoreError) as exc:
                raise StorageError(
                    "could not resolve sqs queue '%s': %s" % (self.queue_name, _error_message(exc))
                ) from exc
            self._queue_url = response.get("QueueUrl")
        if not self._queue_url:
            raise StorageError("sqs queue '%s' has no url" % self.queue_name)
        return self._queue_url

    def send_fulfillment(self, message: Dict[str, Any]) -> str:
        body = json.dumps(message, default=_json_default)
        try:
            response = self.client().send_message(QueueUrl=self.queue_url(), MessageBody=body)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("sqs send_message failed: %s" % _error_message(exc)) from exc
        return response.get("MessageId", "")

    def health(self) -> Dict[str, Any]:
        url = self.queue_url()
        try:
            self.client().get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("sqs queue unavailable: %s" % _error_message(exc)) from exc
        return {"queue_url": url}


class SnsOrderNotifier(OrderNotifier):
    """SNS backed :class:`OrderNotifier`."""

    def __init__(
        self,
        topic_arn: Optional[str] = None,
        topic_name: Optional[str] = None,
        client: Any = None,
    ) -> None:
        self._topic_arn = topic_arn or status_topic_arn()
        self.topic_name = topic_name or status_topic_name()
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            self._client = sns_client()
        return self._client

    def topic_arn(self) -> str:
        if self._topic_arn:
            return self._topic_arn
        suffix = ":" + self.topic_name
        token = None
        try:
            while True:
                kwargs = {"NextToken": token} if token else {}
                response = self.client().list_topics(**kwargs)
                for topic in response.get("Topics", []):
                    arn = topic.get("TopicArn", "")
                    if arn.endswith(suffix):
                        self._topic_arn = arn
                        return arn
                token = response.get("NextToken")
                if not token:
                    break
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("sns list_topics failed: %s" % _error_message(exc)) from exc
        raise StorageError("sns topic '%s' not found" % self.topic_name)

    def publish_status_changed(self, event: Dict[str, Any]) -> str:
        arn = self.topic_arn()
        attributes = {
            "event_type": {
                "DataType": "String",
                "StringValue": str(event.get("event_type") or "order.status_changed"),
            },
            "order_id": {
                "DataType": "String",
                "StringValue": str(event.get("order_id") or "unknown"),
            },
        }
        try:
            response = self.client().publish(
                TopicArn=arn,
                Subject="order-status-changed",
                Message=json.dumps(event, default=_json_default),
                MessageAttributes=attributes,
            )
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("sns publish failed: %s" % _error_message(exc)) from exc
        return response.get("MessageId", "")

    def health(self) -> Dict[str, Any]:
        arn = self.topic_arn()
        try:
            self.client().get_topic_attributes(TopicArn=arn)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("sns topic unavailable: %s" % _error_message(exc)) from exc
        return {"topic_arn": arn}


# --------------------------------------------------------------------------- #
# in-memory implementations (tests / local development)
# --------------------------------------------------------------------------- #
class InMemoryOrderRepository(OrderRepository):
    """Dictionary backed repository used for offline testing."""

    def __init__(self, fail: bool = False) -> None:
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.fail = fail

    def _guard(self) -> None:
        if self.fail:
            raise StorageError("dynamodb unavailable")

    def put_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        self._guard()
        stored = from_dynamo(to_dynamo(order))
        self.orders[stored["order_id"]] = stored
        return dict(stored)

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        self._guard()
        order = self.orders.get(order_id)
        return dict(order) if order else None

    def update_status(
        self,
        order_id: str,
        new_status: str,
        reason: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._guard()
        order = self.orders.get(order_id)
        if order is None:
            raise NotFoundError("order '%s' not found" % order_id)
        order["status"] = new_status
        order["updated_at"] = updated_at or utc_now_iso()
        if reason:
            order["status_reason"] = reason
        return dict(order)

    def list_by_customer(
        self,
        customer_id: str,
        limit: int = 50,
        next_token: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        self._guard()
        items = [dict(order) for order in self.orders.values() if order.get("customer_id") == customer_id]
        if status:
            items = [order for order in items if order.get("status") == status]
        items.sort(key=lambda order: str(order.get("created_at", "")), reverse=True)
        offset = 0
        if next_token:
            offset = int(decode_token(next_token).get("offset", 0))
        page = items[offset:offset + limit]
        token = encode_token({"offset": offset + limit}) if offset + limit < len(items) else None
        return page, token

    def health(self) -> Dict[str, Any]:
        self._guard()
        return {"table": "in-memory", "orders": len(self.orders)}


class InMemoryFulfillmentQueue(FulfillmentQueue):
    """List backed fulfilment queue used for offline testing."""

    def __init__(self, fail: bool = False) -> None:
        self.messages: List[Dict[str, Any]] = []
        self.fail = fail

    def send_fulfillment(self, message: Dict[str, Any]) -> str:
        if self.fail:
            raise StorageError("sqs unavailable")
        payload = json.loads(json.dumps(message, default=_json_default))
        self.messages.append(payload)
        return "in-memory-%d" % len(self.messages)

    def health(self) -> Dict[str, Any]:
        if self.fail:
            raise StorageError("sqs unavailable")
        return {"queue_url": "in-memory", "depth": len(self.messages)}


class InMemoryOrderNotifier(OrderNotifier):
    """List backed SNS notifier used for offline testing."""

    def __init__(self, fail: bool = False) -> None:
        self.published: List[Dict[str, Any]] = []
        self.fail = fail

    def publish_status_changed(self, event: Dict[str, Any]) -> str:
        if self.fail:
            raise StorageError("sns unavailable")
        payload = json.loads(json.dumps(event, default=_json_default))
        self.published.append(payload)
        return "in-memory-%d" % len(self.published)

    def health(self) -> Dict[str, Any]:
        if self.fail:
            raise StorageError("sns unavailable")
        return {"topic_arn": "in-memory", "published": len(self.published)}
