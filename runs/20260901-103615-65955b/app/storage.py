"""AWS data-access layer for the notification hub.

Everything that talks to SNS, SQS or DynamoDB lives behind
:class:`AwsNotificationRepository` so the HTTP layer can be tested with a fake.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key

LOGGER = logging.getLogger("notification_hub.storage")

CHANNELS = ("email", "webhook")

DEFAULT_TOPIC_NAME = "notification-hub-events"
DEFAULT_TABLE_NAME = "notification-hub-subscriptions"
DEFAULT_QUEUE_NAMES = {
    "email": "notification-hub-email-queue",
    "webhook": "notification-hub-webhook-queue",
}


def aws_region() -> str:
    """Region used for every client (defaults to us-east-1)."""
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


def aws_endpoint_url() -> Optional[str]:
    """Optional endpoint override (LocalStack compatibility)."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def sns_client():
    """Build an SNS client."""
    return boto3.client("sns", region_name=aws_region(), endpoint_url=aws_endpoint_url())


def sqs_client():
    """Build an SQS client."""
    return boto3.client("sqs", region_name=aws_region(), endpoint_url=aws_endpoint_url())


def dynamodb_resource():
    """Build a DynamoDB resource."""
    return boto3.resource("dynamodb", region_name=aws_region(), endpoint_url=aws_endpoint_url())


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_ms_to_iso(value: Any) -> str:
    """Convert an SQS SentTimestamp (milliseconds) into ISO-8601 UTC."""
    try:
        seconds = int(value) / 1000.0
    except (TypeError, ValueError):
        return utcnow_iso()
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_message_body(raw: str) -> Dict[str, Any]:
    """Parse an SQS body, unwrapping the SNS envelope when raw delivery is off."""
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": raw}
    if isinstance(body, dict) and body.get("Type") == "Notification" and "Message" in body:
        inner = body.get("Message")
        try:
            parsed = json.loads(inner)
        except (TypeError, ValueError):
            return {"message": inner}
        if isinstance(parsed, dict):
            return parsed
        return {"message": parsed}
    if isinstance(body, dict):
        return body
    return {"message": body}


class AwsNotificationRepository:
    """Small interface over the SNS topic, channel queues and DynamoDB table."""

    def __init__(
        self,
        topic_name: Optional[str] = None,
        table_name: Optional[str] = None,
        queue_names: Optional[Dict[str, str]] = None,
    ) -> None:
        self.topic_name = topic_name or os.environ.get("SNS_TOPIC_NAME", DEFAULT_TOPIC_NAME)
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_TABLE_NAME", DEFAULT_TABLE_NAME
        )
        self.queue_names = queue_names or {
            "email": os.environ.get("EMAIL_QUEUE_NAME", DEFAULT_QUEUE_NAMES["email"]),
            "webhook": os.environ.get("WEBHOOK_QUEUE_NAME", DEFAULT_QUEUE_NAMES["webhook"]),
        }
        self.index_name = os.environ.get("SUBSCRIPTIONS_CHANNEL_INDEX", "channel-index")
        self._topic_arn = os.environ.get("SNS_TOPIC_ARN") or None
        self._sns = None
        self._sqs = None
        self._table = None
        self._queue_urls: Dict[str, str] = {}

    # ------------------------------------------------------------------ clients
    @property
    def sns(self):
        if self._sns is None:
            self._sns = sns_client()
        return self._sns

    @property
    def sqs(self):
        if self._sqs is None:
            self._sqs = sqs_client()
        return self._sqs

    @property
    def table(self):
        if self._table is None:
            self._table = dynamodb_resource().Table(self.table_name)
        return self._table

    # ------------------------------------------------------------------ lookups
    def topic_arn(self) -> str:
        """Resolve (and cache) the ARN of the central topic."""
        if self._topic_arn:
            return self._topic_arn
        suffix = ":" + self.topic_name
        token = None
        while True:
            kwargs = {"NextToken": token} if token else {}
            response = self.sns.list_topics(**kwargs)
            for topic in response.get("Topics", []):
                arn = topic.get("TopicArn", "")
                if arn.endswith(suffix):
                    self._topic_arn = arn
                    return arn
            token = response.get("NextToken")
            if not token:
                break
        created = self.sns.create_topic(Name=self.topic_name)
        self._topic_arn = created["TopicArn"]
        return self._topic_arn

    def queue_name(self, channel: str) -> str:
        """Return the configured queue name of a channel."""
        return self.queue_names[channel]

    def queue_url(self, channel: str) -> str:
        """Resolve (and cache) the queue URL backing a channel."""
        cached = self._queue_urls.get(channel)
        if cached:
            return cached
        response = self.sqs.get_queue_url(QueueName=self.queue_name(channel))
        url = response["QueueUrl"]
        self._queue_urls[channel] = url
        return url

    def queue_attributes(self, channel: str) -> Dict[str, Any]:
        """Return every attribute of the channel queue."""
        response = self.sqs.get_queue_attributes(
            QueueUrl=self.queue_url(channel),
            AttributeNames=["All"],
        )
        return response.get("Attributes", {})

    def queue_arn(self, channel: str) -> str:
        """Return the ARN of the channel queue."""
        return str(self.queue_attributes(channel).get("QueueArn", ""))

    def ensure_channel_subscription(self, channel: str) -> Optional[str]:
        """Make sure the channel queue is subscribed to the topic (idempotent)."""
        response = self.sns.subscribe(
            TopicArn=self.topic_arn(),
            Protocol="sqs",
            Endpoint=self.queue_arn(channel),
            Attributes={"RawMessageDelivery": "true"},
            ReturnSubscriptionArn=True,
        )
        return response.get("SubscriptionArn")

    # ------------------------------------------------------------- subscriptions
    def create_subscription(
        self,
        channel: str,
        target: str,
        event_types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Persist a subscription record and wire the channel queue to the topic."""
        now = utcnow_iso()
        item: Dict[str, Any] = {
            "subscription_id": str(uuid.uuid4()),
            "channel": channel,
            "target": target,
            "event_types": [str(value) for value in (event_types or ["*"])],
            "active": True,
            "created_at": now,
            "updated_at": now,
        }
        self.table.put_item(Item=item)
        result = dict(item)
        try:
            result["sns_subscription_arn"] = self.ensure_channel_subscription(channel)
        except Exception as exc:  # noqa: BLE001 - the record is stored either way
            LOGGER.warning("could not ensure sns subscription: %s", exc.__class__.__name__)
            result["sns_subscription_arn"] = None
        return result

    def _scan_all(self, channel: Optional[str] = None) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {}
        if channel:
            kwargs["FilterExpression"] = Attr("channel").eq(channel)
        items: List[Dict[str, Any]] = []
        while True:
            response = self.table.scan(**kwargs)
            items.extend(response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
            kwargs["ExclusiveStartKey"] = start_key
        return items

    def list_subscriptions(
        self,
        channel: Optional[str] = None,
        target: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List subscription records, optionally filtered by channel/target."""
        items: List[Dict[str, Any]] = []
        if channel:
            try:
                response = self.table.query(
                    IndexName=self.index_name,
                    KeyConditionExpression=Key("channel").eq(channel),
                )
                items = list(response.get("Items", []))
            except Exception as exc:  # noqa: BLE001 - fall back to a table scan
                LOGGER.info("channel index unavailable (%s), scanning", exc.__class__.__name__)
                items = self._scan_all(channel)
        else:
            items = self._scan_all(None)

        if channel:
            items = [item for item in items if item.get("channel") == channel]
        if target:
            items = [item for item in items if item.get("target") == target]
        items.sort(key=lambda item: str(item.get("created_at", "")))
        return [dict(item) for item in items]

    def get_subscription(self, subscription_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single subscription record."""
        response = self.table.get_item(Key={"subscription_id": subscription_id})
        item = response.get("Item")
        return dict(item) if item else None

    def delete_subscription(self, subscription_id: str) -> bool:
        """Delete a subscription record, returning whether it existed."""
        if self.get_subscription(subscription_id) is None:
            return False
        self.table.delete_item(Key={"subscription_id": subscription_id})
        return True

    def count_subscriptions(self, channel: str) -> int:
        """Number of subscriptions registered for a channel."""
        return len(self.list_subscriptions(channel=channel))

    # -------------------------------------------------------------------- events
    def publish_event(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        channel: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Publish an event to the central SNS topic."""
        topic_arn = self.topic_arn()
        published_at = utcnow_iso()
        message = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "channel": channel or "all",
            "published_at": published_at,
            "payload": payload or {},
        }
        attributes = {
            "event_type": {"DataType": "String", "StringValue": event_type},
            "channel": {"DataType": "String", "StringValue": channel or "all"},
        }
        kwargs: Dict[str, Any] = {
            "TopicArn": topic_arn,
            "Message": json.dumps(message),
            "MessageAttributes": attributes,
        }
        if subject:
            kwargs["Subject"] = subject[:100]
        response = self.sns.publish(**kwargs)
        return {
            "message_id": response.get("MessageId", ""),
            "topic_arn": topic_arn,
            "published_at": published_at,
            "event": message,
        }

    def receive_messages(
        self,
        channel: str,
        max_messages: int = 10,
        delete: bool = False,
        wait_seconds: int = 0,
    ) -> List[Dict[str, Any]]:
        """Receive (and optionally delete) messages from a channel queue."""
        queue_url = self.queue_url(channel)
        response = self.sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=max(1, min(10, int(max_messages))),
            WaitTimeSeconds=max(0, min(20, int(wait_seconds))),
            MessageAttributeNames=["All"],
            AttributeNames=["All"],
        )
        results: List[Dict[str, Any]] = []
        for message in response.get("Messages", []):
            body = parse_message_body(message.get("Body", ""))
            attributes = message.get("MessageAttributes") or {}
            event_type = body.get("event_type")
            if not event_type:
                event_type = (attributes.get("event_type") or {}).get("StringValue", "unknown")
            sent_at = (message.get("Attributes") or {}).get("SentTimestamp")
            receipt_handle = message.get("ReceiptHandle", "")
            results.append(
                {
                    "message_id": message.get("MessageId", ""),
                    "receipt_handle": receipt_handle,
                    "event_type": event_type,
                    "body": body,
                    "sent_at": epoch_ms_to_iso(sent_at) if sent_at else utcnow_iso(),
                }
            )
            if delete and receipt_handle:
                self.sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        return results

    # --------------------------------------------------------------- inspection
    def list_channels(self) -> List[Dict[str, Any]]:
        """Describe every supported channel and its backing queue."""
        channels = []
        for channel in CHANNELS:
            channels.append(
                {
                    "channel": channel,
                    "queue_name": self.queue_name(channel),
                    "queue_url": self.queue_url(channel),
                    "queue_arn": self.queue_arn(channel),
                    "subscription_count": self.count_subscriptions(channel),
                }
            )
        return channels

    def stats(self) -> Dict[str, Any]:
        """Per-channel queue metrics plus subscription counts."""
        channels = []
        total_subscriptions = 0
        for channel in CHANNELS:
            attributes = self.queue_attributes(channel)
            available = _as_int(attributes.get("ApproximateNumberOfMessages"))
            not_visible = _as_int(attributes.get("ApproximateNumberOfMessagesNotVisible"))
            delayed = _as_int(attributes.get("ApproximateNumberOfMessagesDelayed"))
            subscription_count = self.count_subscriptions(channel)
            total_subscriptions += subscription_count
            channels.append(
                {
                    "channel": channel,
                    "queue_url": self.queue_url(channel),
                    "approximate_messages_received": available + not_visible + delayed,
                    "approximate_number_of_messages": available,
                    "approximate_number_of_messages_not_visible": not_visible,
                    "approximate_number_of_messages_delayed": delayed,
                    "subscription_count": subscription_count,
                }
            )
        return {
            "topic_name": self.topic_name,
            "channels": channels,
            "total_subscriptions": total_subscriptions,
        }

    def health(self) -> Dict[str, str]:
        """Probe SNS, SQS and DynamoDB."""
        checks: Dict[str, str] = {}
        try:
            self.sns.get_topic_attributes(TopicArn=self.topic_arn())
            checks["sns"] = "ok"
        except Exception as exc:  # noqa: BLE001 - report instead of raising
            checks["sns"] = "error: {}".format(exc.__class__.__name__)
        try:
            for channel in CHANNELS:
                self.queue_attributes(channel)
            checks["sqs"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["sqs"] = "error: {}".format(exc.__class__.__name__)
        try:
            _ = self.table.table_status
            checks["dynamodb"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["dynamodb"] = "error: {}".format(exc.__class__.__name__)
        return checks


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
