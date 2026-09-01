"""AWS data-access layer for the notification hub.

All boto3 usage lives behind :class:`NotificationRepository` so that the API
layer can be tested with a fake repository and so that clients can be pointed
at LocalStack through the ``AWS_ENDPOINT_URL`` environment variable.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

CHANNELS = ("email", "webhook")

DEFAULT_TOPIC_NAME = "notification-hub-events-topic"
DEFAULT_TABLE_NAME = "notification-hub-subscriptions"
DEFAULT_CHANNEL_INDEX = "channel-created_at-index"
DEFAULT_QUEUE_NAMES = {
    "email": "notification-hub-email-queue",
    "webhook": "notification-hub-webhook-queue",
}
QUEUE_URL_ENV = {"email": "EMAIL_QUEUE_URL", "webhook": "WEBHOOK_QUEUE_URL"}
QUEUE_NAME_ENV = {"email": "EMAIL_QUEUE_NAME", "webhook": "WEBHOOK_QUEUE_NAME"}

STAT_ATTRIBUTES = [
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
]

UPDATABLE_FIELDS = ("target", "event_types", "active")


class NotFoundError(Exception):
    """Raised when a requested record or resource does not exist."""


class DependencyError(Exception):
    """Raised when a call to an AWS dependency fails."""


def region_name() -> str:
    """Return the configured AWS region, defaulting to us-east-1."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def endpoint_url() -> Optional[str]:
    """Return the AWS endpoint override (LocalStack) when configured."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def sns_client():
    """Build an SNS client."""
    return boto3.client("sns", region_name=region_name(), endpoint_url=endpoint_url())


def sqs_client():
    """Build an SQS client."""
    return boto3.client("sqs", region_name=region_name(), endpoint_url=endpoint_url())


def dynamodb_resource():
    """Build a DynamoDB resource."""
    return boto3.resource("dynamodb", region_name=region_name(), endpoint_url=endpoint_url())


def utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _error_code(exc: Exception) -> str:
    """Extract the AWS error code from a botocore style exception."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        return str(error.get("Code") or "")
    return ""


def _message(exc: Exception) -> str:
    """Human readable message for an exception."""
    return str(exc) or exc.__class__.__name__


def _as_int(value: Any) -> int:
    """Best effort conversion of an SQS attribute value to int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def decode_message_body(raw: Any) -> Dict[str, Any]:
    """Unwrap an SNS envelope from a raw SQS message body."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": raw}
    if isinstance(parsed, dict):
        if "Message" in parsed and "Type" in parsed:
            inner = parsed.get("Message")
            try:
                inner_parsed = json.loads(inner)
            except (TypeError, ValueError):
                return {"message": inner}
            if isinstance(inner_parsed, dict):
                return inner_parsed
            return {"message": inner_parsed}
        return parsed
    return {"value": parsed}


def normalise_subscription(item: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a stored DynamoDB item into the API subscription shape."""
    event_types = item.get("event_types") or []
    if not isinstance(event_types, list):
        event_types = [str(event_types)]
    return {
        "subscription_id": str(item.get("subscription_id", "")),
        "channel": str(item.get("channel", "")),
        "target": str(item.get("target", "")),
        "event_types": [str(value) for value in event_types],
        "active": bool(item.get("active", True)),
        "created_at": str(item.get("created_at", "")),
        "updated_at": str(item.get("updated_at", item.get("created_at", ""))),
    }


class NotificationRepository:
    """Thin interface over SNS, SQS and DynamoDB used by the API layer."""

    def __init__(self) -> None:
        self._sns = None
        self._sqs = None
        self._dynamodb = None
        self._table_obj = None
        self._topic_arn: Optional[str] = None
        self._queue_urls: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # lazy clients / resource names
    # ------------------------------------------------------------------
    def sns(self):
        if self._sns is None:
            self._sns = sns_client()
        return self._sns

    def sqs(self):
        if self._sqs is None:
            self._sqs = sqs_client()
        return self._sqs

    def table(self):
        if self._table_obj is None:
            if self._dynamodb is None:
                self._dynamodb = dynamodb_resource()
            name = os.environ.get("SUBSCRIPTIONS_TABLE", DEFAULT_TABLE_NAME)
            self._table_obj = self._dynamodb.Table(name)
        return self._table_obj

    def channel_index(self) -> str:
        return os.environ.get("SUBSCRIPTIONS_CHANNEL_INDEX", DEFAULT_CHANNEL_INDEX)

    def topic_arn(self) -> str:
        """Resolve the central SNS topic ARN."""
        if self._topic_arn:
            return self._topic_arn
        configured = os.environ.get("SNS_TOPIC_ARN")
        if configured:
            self._topic_arn = configured
            return configured
        name = os.environ.get("SNS_TOPIC_NAME", DEFAULT_TOPIC_NAME)
        token = None
        try:
            while True:
                kwargs = {"NextToken": token} if token else {}
                response = self.sns().list_topics(**kwargs)
                for topic in response.get("Topics") or []:
                    arn = topic.get("TopicArn", "")
                    if arn.rsplit(":", 1)[-1] == name:
                        self._topic_arn = arn
                        return arn
                token = response.get("NextToken")
                if not token:
                    break
        except Exception as exc:
            raise DependencyError("unable to resolve SNS topic: {}".format(_message(exc))) from exc
        raise DependencyError("SNS topic '{}' not found".format(name))

    def queue_url(self, channel: str) -> str:
        """Resolve the SQS queue URL backing a channel."""
        if channel not in CHANNELS:
            raise NotFoundError("unknown channel '{}'".format(channel))
        cached = self._queue_urls.get(channel)
        if cached:
            return cached
        configured = os.environ.get(QUEUE_URL_ENV[channel])
        if configured:
            self._queue_urls[channel] = configured
            return configured
        name = os.environ.get(QUEUE_NAME_ENV[channel], DEFAULT_QUEUE_NAMES[channel])
        try:
            response = self.sqs().get_queue_url(QueueName=name)
        except Exception as exc:
            raise DependencyError(
                "unable to resolve queue for channel '{}': {}".format(channel, _message(exc))
            ) from exc
        url = response.get("QueueUrl", "")
        if not url:
            raise DependencyError("queue '{}' not found".format(name))
        self._queue_urls[channel] = url
        return url

    def channel_queue_urls(self) -> Dict[str, str]:
        """Return a mapping of channel name to queue URL."""
        return {channel: self.queue_url(channel) for channel in CHANNELS}

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def publish_event(
        self,
        event_type: str,
        subject: Optional[str],
        payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Publish an event to the central SNS topic."""
        arn = self.topic_arn()
        event = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "subject": subject,
            "payload": payload or {},
            "published_at": utcnow(),
        }
        kwargs: Dict[str, Any] = {
            "TopicArn": arn,
            "Message": json.dumps(event),
            "MessageAttributes": {
                "event_type": {"DataType": "String", "StringValue": event_type},
            },
        }
        if subject:
            kwargs["Subject"] = subject[:100]
        try:
            response = self.sns().publish(**kwargs)
        except Exception as exc:
            raise DependencyError("failed to publish event: {}".format(_message(exc))) from exc
        event["sns_message_id"] = str(response.get("MessageId", ""))
        return event

    # ------------------------------------------------------------------
    # subscriptions
    # ------------------------------------------------------------------
    def create_subscription(
        self,
        channel: str,
        target: str,
        event_types: Optional[List[str]] = None,
        active: bool = True,
    ) -> Dict[str, Any]:
        """Persist a new subscription record."""
        if channel not in CHANNELS:
            raise NotFoundError("unknown channel '{}'".format(channel))
        now = utcnow()
        item = {
            "subscription_id": str(uuid.uuid4()),
            "channel": channel,
            "target": target,
            "event_types": list(event_types or []),
            "active": bool(active),
            "created_at": now,
            "updated_at": now,
        }
        try:
            self.table().put_item(Item=item)
        except Exception as exc:
            raise DependencyError("failed to store subscription: {}".format(_message(exc))) from exc
        return normalise_subscription(item)

    def get_subscription(self, subscription_id: str) -> Dict[str, Any]:
        """Fetch a subscription by id."""
        try:
            response = self.table().get_item(Key={"subscription_id": subscription_id})
        except Exception as exc:
            raise DependencyError("failed to read subscription: {}".format(_message(exc))) from exc
        item = response.get("Item")
        if not item:
            raise NotFoundError("subscription '{}' not found".format(subscription_id))
        return normalise_subscription(item)

    def list_subscriptions(self, channel: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """List subscriptions, optionally filtered by channel."""
        if channel is not None and channel not in CHANNELS:
            raise NotFoundError("unknown channel '{}'".format(channel))
        table = self.table()
        items: List[Dict[str, Any]] = []
        try:
            if channel:
                items = self._query_by_channel(table, channel, limit)
            else:
                response = table.scan(Limit=limit)
                items = list(response.get("Items") or [])
        except DependencyError:
            raise
        except Exception as exc:
            raise DependencyError("failed to list subscriptions: {}".format(_message(exc))) from exc
        records = [normalise_subscription(item) for item in items]
        records.sort(key=lambda record: (record.get("created_at", ""), record.get("subscription_id", "")))
        return records[:limit]

    def _query_by_channel(self, table, channel: str, limit: int) -> List[Dict[str, Any]]:
        """Query the channel GSI, falling back to a filtered scan."""
        try:
            response = table.query(
                IndexName=self.channel_index(),
                KeyConditionExpression="#c = :c",
                ExpressionAttributeNames={"#c": "channel"},
                ExpressionAttributeValues={":c": channel},
                Limit=limit,
            )
            return list(response.get("Items") or [])
        except Exception:
            response = table.scan(
                FilterExpression="#c = :c",
                ExpressionAttributeNames={"#c": "channel"},
                ExpressionAttributeValues={":c": channel},
                Limit=limit,
            )
            return list(response.get("Items") or [])

    def update_subscription(self, subscription_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a partial update to a subscription."""
        fields = {key: value for key, value in updates.items() if key in UPDATABLE_FIELDS}
        if not fields:
            raise NotFoundError("no updatable fields supplied")
        fields["updated_at"] = utcnow()
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        assignments = []
        for index, (field, value) in enumerate(sorted(fields.items())):
            names["#n{}".format(index)] = field
            values[":n{}".format(index)] = value
            assignments.append("#n{} = :n{}".format(index, index))
        try:
            response = self.table().update_item(
                Key={"subscription_id": subscription_id},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(subscription_id)",
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if _error_code(exc) == "ConditionalCheckFailedException":
                raise NotFoundError("subscription '{}' not found".format(subscription_id)) from exc
            raise DependencyError("failed to update subscription: {}".format(_message(exc))) from exc
        item = response.get("Attributes") or {}
        item.setdefault("subscription_id", subscription_id)
        return normalise_subscription(item)

    def delete_subscription(self, subscription_id: str) -> None:
        """Delete a subscription record."""
        try:
            self.table().delete_item(
                Key={"subscription_id": subscription_id},
                ConditionExpression="attribute_exists(subscription_id)",
            )
        except Exception as exc:
            if _error_code(exc) == "ConditionalCheckFailedException":
                raise NotFoundError("subscription '{}' not found".format(subscription_id)) from exc
            raise DependencyError("failed to delete subscription: {}".format(_message(exc))) from exc

    # ------------------------------------------------------------------
    # channel queues
    # ------------------------------------------------------------------
    def channel_stats(self, channel: str) -> Dict[str, Any]:
        """Read the message counters for a channel queue."""
        url = self.queue_url(channel)
        try:
            response = self.sqs().get_queue_attributes(QueueUrl=url, AttributeNames=STAT_ATTRIBUTES)
        except Exception as exc:
            raise DependencyError(
                "failed to read attributes for channel '{}': {}".format(channel, _message(exc))
            ) from exc
        attributes = response.get("Attributes") or {}
        available = _as_int(attributes.get("ApproximateNumberOfMessages"))
        in_flight = _as_int(attributes.get("ApproximateNumberOfMessagesNotVisible"))
        delayed = _as_int(attributes.get("ApproximateNumberOfMessagesDelayed"))
        return {
            "channel": channel,
            "queue_url": url,
            "messages_available": available,
            "messages_in_flight": in_flight,
            "messages_delayed": delayed,
            "total_received": available + in_flight + delayed,
            "collected_at": utcnow(),
        }

    def all_channel_stats(self) -> List[Dict[str, Any]]:
        """Read the message counters for every channel queue."""
        return [self.channel_stats(channel) for channel in CHANNELS]

    def receive_messages(
        self,
        channel: str,
        max_messages: int = 10,
        wait_time_seconds: int = 0,
        delete: bool = False,
    ) -> List[Dict[str, Any]]:
        """Receive, and optionally delete, messages from a channel queue."""
        url = self.queue_url(channel)
        try:
            response = self.sqs().receive_message(
                QueueUrl=url,
                MaxNumberOfMessages=max(1, min(int(max_messages), 10)),
                WaitTimeSeconds=max(0, min(int(wait_time_seconds), 20)),
                AttributeNames=["All"],
                MessageAttributeNames=["All"],
            )
        except Exception as exc:
            raise DependencyError(
                "failed to receive messages for channel '{}': {}".format(channel, _message(exc))
            ) from exc
        messages = []
        for raw in response.get("Messages") or []:
            messages.append(
                {
                    "message_id": str(raw.get("MessageId", "")),
                    "receipt_handle": str(raw.get("ReceiptHandle", "")),
                    "body": decode_message_body(raw.get("Body")),
                    "attributes": dict(raw.get("Attributes") or {}),
                }
            )
        if delete and messages:
            for message in messages:
                handle = message["receipt_handle"]
                if not handle:
                    continue
                try:
                    self.sqs().delete_message(QueueUrl=url, ReceiptHandle=handle)
                except Exception as exc:
                    raise DependencyError(
                        "failed to delete message from channel '{}': {}".format(channel, _message(exc))
                    ) from exc
        return messages

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------
    def health(self) -> Dict[str, str]:
        """Check reachability of every AWS dependency."""
        checks: Dict[str, str] = {}
        try:
            self.sns().get_topic_attributes(TopicArn=self.topic_arn())
            checks["sns"] = "ok"
        except Exception as exc:
            checks["sns"] = "error: {}".format(_message(exc))
        try:
            for channel in CHANNELS:
                self.sqs().get_queue_attributes(
                    QueueUrl=self.queue_url(channel), AttributeNames=["QueueArn"]
                )
            checks["sqs"] = "ok"
        except Exception as exc:
            checks["sqs"] = "error: {}".format(_message(exc))
        try:
            self.table().load()
            checks["dynamodb"] = "ok"
        except Exception as exc:
            checks["dynamodb"] = "error: {}".format(_message(exc))
        return checks
