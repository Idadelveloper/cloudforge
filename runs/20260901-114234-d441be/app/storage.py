"""AWS data-access layer for the loyalty points service.

Everything that touches DynamoDB, SQS, SNS or S3 lives here behind the small
``LoyaltyRepository`` interface so the HTTP layer and the accrual logic stay
testable without any network access.
"""
import base64
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_CUSTOMERS_TABLE = "loyalty-customers"
DEFAULT_TRANSACTIONS_TABLE = "loyalty-transactions"
DEFAULT_IDEMPOTENCY_TABLE = "loyalty-idempotency"
DEFAULT_QUEUE_NAME = "loyalty-purchases-queue"
DEFAULT_TOPIC_NAME = "loyalty-gold-tier-upgrades"
DEFAULT_AUDIT_BUCKET = "loyalty-audit-log"


class CustomerNotFound(Exception):
    """Raised when an update targets a customer that does not exist."""


def region_name() -> str:
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or DEFAULT_REGION


def endpoint_url() -> Optional[str]:
    return os.environ.get("AWS_ENDPOINT_URL") or None


def boto_client(service: str):
    return boto3.client(service, region_name=region_name(), endpoint_url=endpoint_url())


def boto_resource(service: str):
    return boto3.resource(service, region_name=region_name(), endpoint_url=endpoint_url())


def utc_now_iso() -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def to_native(value: Any) -> Any:
    """Convert DynamoDB Decimals (and containers) into plain JSON types."""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: to_native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_native(item) for item in value]
    if isinstance(value, set):
        return [to_native(item) for item in value]
    return value


def encode_cursor(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> Dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(str(cursor).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid cursor")
    return payload


def _is_conditional_failure(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code")
    return code == "ConditionalCheckFailedException"


class LoyaltyRepository:
    """Concrete AWS-backed repository."""

    def __init__(self) -> None:
        self.customers_table_name = os.environ.get("LOYALTY_CUSTOMERS_TABLE", DEFAULT_CUSTOMERS_TABLE)
        self.transactions_table_name = os.environ.get("LOYALTY_TRANSACTIONS_TABLE", DEFAULT_TRANSACTIONS_TABLE)
        self.idempotency_table_name = os.environ.get("LOYALTY_IDEMPOTENCY_TABLE", DEFAULT_IDEMPOTENCY_TABLE)
        self.queue_name = os.environ.get("LOYALTY_QUEUE_NAME", DEFAULT_QUEUE_NAME)
        self.topic_name = os.environ.get("LOYALTY_TOPIC_NAME", DEFAULT_TOPIC_NAME)
        self.audit_bucket = os.environ.get("LOYALTY_AUDIT_BUCKET", DEFAULT_AUDIT_BUCKET)
        self.queue_url_override = os.environ.get("LOYALTY_QUEUE_URL") or None
        self.topic_arn_override = os.environ.get("LOYALTY_TOPIC_ARN") or None
        self._dynamodb = None
        self._sqs = None
        self._sns = None
        self._s3 = None
        self._queue_url: Optional[str] = None
        self._topic_arn: Optional[str] = None

    # ------------------------------------------------------------------ AWS
    @property
    def dynamodb(self):
        if self._dynamodb is None:
            self._dynamodb = boto_resource("dynamodb")
        return self._dynamodb

    @property
    def sqs(self):
        if self._sqs is None:
            self._sqs = boto_client("sqs")
        return self._sqs

    @property
    def sns(self):
        if self._sns is None:
            self._sns = boto_client("sns")
        return self._sns

    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = boto_client("s3")
        return self._s3

    @property
    def customers_table(self):
        return self.dynamodb.Table(self.customers_table_name)

    @property
    def transactions_table(self):
        return self.dynamodb.Table(self.transactions_table_name)

    @property
    def idempotency_table(self):
        return self.dynamodb.Table(self.idempotency_table_name)

    def queue_url(self) -> str:
        if self.queue_url_override:
            return self.queue_url_override
        if self._queue_url is None:
            self._queue_url = self.sqs.get_queue_url(QueueName=self.queue_name)["QueueUrl"]
        return self._queue_url

    def topic_arn(self) -> str:
        if self.topic_arn_override:
            return self.topic_arn_override
        if self._topic_arn is None:
            suffix = ":" + self.topic_name
            paginator = self.sns.get_paginator("list_topics")
            for page in paginator.paginate():
                for topic in page.get("Topics", []):
                    arn = topic.get("TopicArn", "")
                    if arn.endswith(suffix):
                        self._topic_arn = arn
                        break
                if self._topic_arn:
                    break
        if self._topic_arn is None:
            raise RuntimeError("SNS topic {0} not found".format(self.topic_name))
        return self._topic_arn

    # --------------------------------------------------------------- health
    def health(self) -> Dict[str, str]:
        checks = (
            ("dynamodb", self._check_dynamodb),
            ("sqs", self._check_sqs),
            ("sns", self._check_sns),
            ("s3", self._check_s3),
        )
        statuses: Dict[str, str] = {}
        for name, check in checks:
            try:
                check()
                statuses[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - a probe must never raise
                LOGGER.warning("health check failed for %s: %s", name, exc)
                statuses[name] = "unavailable"
        return statuses

    def _check_dynamodb(self) -> None:
        client = self.dynamodb.meta.client
        for name in (
            self.customers_table_name,
            self.transactions_table_name,
            self.idempotency_table_name,
        ):
            client.describe_table(TableName=name)

    def _check_sqs(self) -> None:
        self.sqs.get_queue_attributes(QueueUrl=self.queue_url(), AttributeNames=["QueueArn"])

    def _check_sns(self) -> None:
        self.sns.get_topic_attributes(TopicArn=self.topic_arn())

    def _check_s3(self) -> None:
        self.s3.head_bucket(Bucket=self.audit_bucket)

    # ------------------------------------------------------------ customers
    def create_customer(self, item: Dict[str, Any]) -> bool:
        try:
            self.customers_table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(customer_id)",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise
        return True

    def get_customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        response = self.customers_table.get_item(Key={"customer_id": customer_id})
        item = response.get("Item")
        return to_native(item) if item else None

    def increment_points(self, customer_id: str, points: int) -> Dict[str, Any]:
        """Atomically ADD points to the balance; returns before/after state."""
        delta = Decimal(int(points))
        try:
            response = self.customers_table.update_item(
                Key={"customer_id": customer_id},
                UpdateExpression="ADD points_balance :delta, lifetime_points :delta SET updated_at = :now",
                ConditionExpression="attribute_exists(customer_id)",
                ExpressionAttributeValues={":delta": delta, ":now": utc_now_iso()},
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise CustomerNotFound(customer_id) from exc
            raise
        item = to_native(response.get("Attributes", {}))
        balance_after = int(item.get("points_balance", 0))
        return {
            "balance_before": balance_after - int(points),
            "balance_after": balance_after,
            "tier_before": item.get("tier", "standard"),
            "lifetime_points": int(item.get("lifetime_points", 0)),
            "customer": item,
        }

    def upgrade_tier(self, customer_id: str) -> bool:
        """Conditionally move standard -> gold.  True only for the winner."""
        try:
            self.customers_table.update_item(
                Key={"customer_id": customer_id},
                UpdateExpression="SET #tier = :gold, updated_at = :now",
                ConditionExpression="#tier = :standard",
                ExpressionAttributeNames={"#tier": "tier"},
                ExpressionAttributeValues={
                    ":gold": "gold",
                    ":standard": "standard",
                    ":now": utc_now_iso(),
                },
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise
        return True

    # ---------------------------------------------------------- idempotency
    def reserve_idempotency(self, record: Dict[str, Any]) -> bool:
        try:
            self.idempotency_table.put_item(
                Item=record,
                ConditionExpression="attribute_not_exists(idempotency_key)",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise
        return True

    def get_idempotency(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        response = self.idempotency_table.get_item(Key={"idempotency_key": idempotency_key})
        item = response.get("Item")
        return to_native(item) if item else None

    def begin_processing(self, idempotency_key: str) -> bool:
        """Claim a pending record for processing (guards double awards)."""
        try:
            self.idempotency_table.update_item(
                Key={"idempotency_key": idempotency_key},
                UpdateExpression="SET #status = :processing, updated_at = :now",
                ConditionExpression="attribute_exists(idempotency_key) AND #status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "processing",
                    ":pending": "pending",
                    ":now": utc_now_iso(),
                },
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise
        return True

    def finish_idempotency(
        self,
        idempotency_key: str,
        status: str,
        transaction_id: Optional[str] = None,
        points_awarded: Optional[int] = None,
    ) -> Dict[str, Any]:
        response = self.idempotency_table.update_item(
            Key={"idempotency_key": idempotency_key},
            UpdateExpression=(
                "SET #status = :status, transaction_id = :txid, "
                "points_awarded = :points, updated_at = :now"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": status,
                ":txid": transaction_id,
                ":points": None if points_awarded is None else Decimal(int(points_awarded)),
                ":now": utc_now_iso(),
            },
            ReturnValues="ALL_NEW",
        )
        return to_native(response.get("Attributes", {}))

    # --------------------------------------------------------- transactions
    def put_transaction(self, transaction: Dict[str, Any]) -> None:
        item = {key: value for key, value in transaction.items() if value is not None}
        self.transactions_table.put_item(Item=item)

    def list_transactions(
        self,
        customer_id: str,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("customer_id").eq(customer_id),
            "ScanIndexForward": False,
            "Limit": int(limit),
        }
        if cursor:
            kwargs["ExclusiveStartKey"] = decode_cursor(cursor)
        response = self.transactions_table.query(**kwargs)
        items = [to_native(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        next_cursor = encode_cursor(to_native(last_key)) if last_key else None
        return items, next_cursor

    # ------------------------------------------------------------ messaging
    def enqueue_purchase(self, message: Dict[str, Any]) -> str:
        response = self.sqs.send_message(
            QueueUrl=self.queue_url(),
            MessageBody=json.dumps(message, default=str),
        )
        return response.get("MessageId", "")

    def receive_purchases(self, max_messages: int = 10) -> List[Tuple[str, Dict[str, Any]]]:
        response = self.sqs.receive_message(
            QueueUrl=self.queue_url(),
            MaxNumberOfMessages=max(1, min(10, int(max_messages))),
            WaitTimeSeconds=int(os.environ.get("LOYALTY_SQS_WAIT_SECONDS", "1")),
            VisibilityTimeout=int(os.environ.get("LOYALTY_SQS_VISIBILITY_TIMEOUT", "30")),
        )
        batch: List[Tuple[str, Dict[str, Any]]] = []
        for message in response.get("Messages", []):
            handle = message.get("ReceiptHandle", "")
            try:
                body = json.loads(message.get("Body", "{}"))
            except ValueError:
                LOGGER.warning("discarding non-JSON SQS message %s", message.get("MessageId"))
                body = {}
            if not isinstance(body, dict):
                body = {}
            batch.append((handle, body))
        return batch

    def delete_message(self, receipt_handle: str) -> None:
        self.sqs.delete_message(QueueUrl=self.queue_url(), ReceiptHandle=receipt_handle)

    def publish_gold_upgrade(self, payload: Dict[str, Any]) -> str:
        response = self.sns.publish(
            TopicArn=self.topic_arn(),
            Subject="Loyalty gold tier upgrade",
            Message=json.dumps(payload, default=str),
            MessageAttributes={
                "event_type": {"DataType": "String", "StringValue": "tier_upgraded"},
            },
        )
        return response.get("MessageId", "")

    # ---------------------------------------------------------- audit trail
    def put_audit_entry(self, entry: Dict[str, Any]) -> str:
        recorded_at = str(entry.get("recorded_at") or utc_now_iso())
        parts = recorded_at[:10].split("-")
        if len(parts) != 3:
            parts = utc_now_iso()[:10].split("-")
        suffix = str(entry.get("transaction_id") or entry.get("event_id") or "unknown")
        event_type = str(entry.get("event_type") or "points_accrued")
        if event_type != "points_accrued":
            suffix = "{0}-{1}".format(suffix, event_type)
        key = "audit/{0}/{1}/{2}/{3}/{4}.json".format(
            entry.get("customer_id", "unknown"), parts[0], parts[1], parts[2], suffix
        )
        self.s3.put_object(
            Bucket=self.audit_bucket,
            Key=key,
            Body=json.dumps(entry, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        return key
