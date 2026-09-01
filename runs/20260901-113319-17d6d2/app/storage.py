"""AWS data-access layer for the loyalty points service.

Every boto3 client honours the ``AWS_ENDPOINT_URL`` environment variable so the
service works unchanged against LocalStack, and defaults to region us-east-1.
All resource names come from environment variables with defaults that match the
provisioned infrastructure.
"""

import base64
import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

DEFAULT_REGION = "us-east-1"
DEFAULT_CUSTOMERS_TABLE = "loyalty-customers"
DEFAULT_TRANSACTIONS_TABLE = "loyalty-transactions"
DEFAULT_IDEMPOTENCY_TABLE = "loyalty-idempotency"
DEFAULT_QUEUE_NAME = "loyalty-purchases-queue"
DEFAULT_BUCKET = "loyalty-audit-log"
DEFAULT_TOPIC_NAME = "loyalty-tier-upgrades"

IDEMPOTENCY_TTL_SECONDS = 30 * 24 * 60 * 60


def region_name():
    """Resolve the AWS region for every client."""
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or DEFAULT_REGION


def endpoint_url():
    """Resolve the (optional) LocalStack endpoint override."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource():
    """Create a DynamoDB resource."""
    return boto3.resource("dynamodb", region_name=region_name(), endpoint_url=endpoint_url())


def sqs_client():
    """Create an SQS client."""
    return boto3.client("sqs", region_name=region_name(), endpoint_url=endpoint_url())


def sns_client():
    """Create an SNS client."""
    return boto3.client("sns", region_name=region_name(), endpoint_url=endpoint_url())


def s3_client():
    """Create an S3 client."""
    return boto3.client("s3", region_name=region_name(), endpoint_url=endpoint_url())


def utcnow_iso():
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_transaction_id():
    """Time-ordered transaction id so range keys sort chronologically."""
    return "{0:013d}-{1}".format(int(time.time() * 1000), uuid.uuid4().hex[:12])


def json_safe(value):
    """Recursively convert DynamoDB Decimals into JSON-friendly numbers."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def encode_cursor(last_key):
    """Encode a DynamoDB LastEvaluatedKey as an opaque cursor."""
    if not last_key:
        return None
    raw = json.dumps(json_safe(last_key), separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor):
    """Decode an opaque cursor, raising ValueError when it is malformed."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ValueError("invalid cursor")
    if not isinstance(decoded, dict):
        raise ValueError("invalid cursor")
    return decoded


def audit_key(customer_id, recorded_at, transaction_id):
    """Build the S3 object key for an audit-log entry."""
    year, month, day = recorded_at[0:4], recorded_at[5:7], recorded_at[8:10]
    stamp = "".join(char for char in recorded_at if char.isalnum())
    return "customers/{0}/{1}/{2}/{3}/{4}-{5}.json".format(
        customer_id, year, month, day, stamp, transaction_id
    )


class LoyaltyRepository(object):
    """boto3-backed persistence for customers, transactions, audit log and queue."""

    def __init__(
        self,
        customers_table=None,
        transactions_table=None,
        idempotency_table=None,
        queue_url=None,
        queue_name=None,
        bucket=None,
        topic_arn=None,
        topic_name=None,
    ):
        env = os.environ
        self.customers_table_name = customers_table or env.get("CUSTOMERS_TABLE", DEFAULT_CUSTOMERS_TABLE)
        self.transactions_table_name = transactions_table or env.get(
            "TRANSACTIONS_TABLE", DEFAULT_TRANSACTIONS_TABLE
        )
        self.idempotency_table_name = idempotency_table or env.get(
            "IDEMPOTENCY_TABLE", DEFAULT_IDEMPOTENCY_TABLE
        )
        self.queue_name = queue_name or env.get("PURCHASES_QUEUE_NAME", DEFAULT_QUEUE_NAME)
        self.bucket = bucket or env.get("AUDIT_BUCKET", DEFAULT_BUCKET)
        self.topic_name = topic_name or env.get("TIER_TOPIC_NAME", DEFAULT_TOPIC_NAME)
        self._queue_url = queue_url or env.get("PURCHASES_QUEUE_URL") or None
        self._topic_arn = topic_arn or env.get("TIER_TOPIC_ARN") or None
        self._dynamodb = None
        self._sqs = None
        self._sns = None
        self._s3 = None

    # ------------------------------------------------------------------
    # lazily created clients
    # ------------------------------------------------------------------
    @property
    def dynamodb(self):
        if self._dynamodb is None:
            self._dynamodb = dynamodb_resource()
        return self._dynamodb

    @property
    def sqs(self):
        if self._sqs is None:
            self._sqs = sqs_client()
        return self._sqs

    @property
    def sns(self):
        if self._sns is None:
            self._sns = sns_client()
        return self._sns

    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = s3_client()
        return self._s3

    @property
    def customers(self):
        return self.dynamodb.Table(self.customers_table_name)

    @property
    def transactions(self):
        return self.dynamodb.Table(self.transactions_table_name)

    @property
    def idempotency(self):
        return self.dynamodb.Table(self.idempotency_table_name)

    @property
    def conditional_failed(self):
        """Exception class raised when a DynamoDB condition is not met."""
        return self.dynamodb.meta.client.exceptions.ConditionalCheckFailedException

    @property
    def queue_url(self):
        if not self._queue_url:
            self._queue_url = self.sqs.get_queue_url(QueueName=self.queue_name)["QueueUrl"]
        return self._queue_url

    @property
    def topic_arn(self):
        if not self._topic_arn:
            self._topic_arn = self._resolve_topic_arn()
        return self._topic_arn

    def _resolve_topic_arn(self):
        token = None
        while True:
            kwargs = {"NextToken": token} if token else {}
            response = self.sns.list_topics(**kwargs)
            for topic in response.get("Topics", []):
                arn = topic.get("TopicArn", "")
                if arn.rsplit(":", 1)[-1] == self.topic_name:
                    return arn
            token = response.get("NextToken")
            if not token:
                break
        return self.sns.create_topic(Name=self.topic_name)["TopicArn"]

    # ------------------------------------------------------------------
    # customers
    # ------------------------------------------------------------------
    def create_customer(self, customer_id, email, name):
        """Create a customer; return None when the id already exists."""
        now = utcnow_iso()
        item = {
            "customer_id": customer_id,
            "email": email,
            "name": name,
            "points_balance": 0,
            "tier": "standard",
            "created_at": now,
            "updated_at": now,
        }
        try:
            self.customers.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(customer_id)",
            )
        except self.conditional_failed:
            return None
        return item

    def get_customer(self, customer_id):
        """Return a customer record or None."""
        response = self.customers.get_item(Key={"customer_id": customer_id})
        item = response.get("Item")
        return json_safe(item) if item else None

    def increment_balance(self, customer_id, points):
        """Atomically add points; return the updated record or None if missing."""
        try:
            response = self.customers.update_item(
                Key={"customer_id": customer_id},
                UpdateExpression="SET updated_at = :now ADD points_balance :points",
                ConditionExpression="attribute_exists(customer_id)",
                ExpressionAttributeValues={
                    ":points": Decimal(int(points)),
                    ":now": utcnow_iso(),
                },
                ReturnValues="ALL_NEW",
            )
        except self.conditional_failed:
            return None
        return json_safe(response.get("Attributes") or {})

    def upgrade_tier(self, customer_id, new_tier="gold"):
        """Conditionally move a customer to a new tier; True when it changed."""
        try:
            self.customers.update_item(
                Key={"customer_id": customer_id},
                UpdateExpression="SET #tier = :new, updated_at = :now",
                ConditionExpression="attribute_exists(customer_id) AND #tier <> :new",
                ExpressionAttributeNames={"#tier": "tier"},
                ExpressionAttributeValues={":new": new_tier, ":now": utcnow_iso()},
            )
        except self.conditional_failed:
            return False
        return True

    # ------------------------------------------------------------------
    # transactions
    # ------------------------------------------------------------------
    def put_transaction(self, item):
        """Persist a transaction record."""
        self.transactions.put_item(Item=item)
        return item

    def update_transaction(self, customer_id, transaction_id, status, points_awarded=None, balance_after=None):
        """Update the outcome of a transaction."""
        assignments = ["#status = :status", "updated_at = :now"]
        values = {":status": status, ":now": utcnow_iso()}
        if points_awarded is not None:
            assignments.append("points_awarded = :points")
            values[":points"] = Decimal(int(points_awarded))
        if balance_after is not None:
            assignments.append("balance_after = :balance")
            values[":balance"] = Decimal(int(balance_after))
        self.transactions.update_item(
            Key={"customer_id": customer_id, "transaction_id": transaction_id},
            UpdateExpression="SET " + ", ".join(assignments),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
        )

    def list_transactions(self, customer_id, limit=50, cursor=None):
        """Return (items, next_cursor) newest first."""
        kwargs = {
            "KeyConditionExpression": Key("customer_id").eq(customer_id),
            "ScanIndexForward": False,
            "Limit": int(limit),
        }
        start_key = decode_cursor(cursor)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = self.transactions.query(**kwargs)
        items = [json_safe(item) for item in response.get("Items", [])]
        return items, encode_cursor(response.get("LastEvaluatedKey"))

    # ------------------------------------------------------------------
    # idempotency
    # ------------------------------------------------------------------
    def reserve_idempotency_record(self, key, customer_id, transaction_id, payload):
        """Conditionally reserve an idempotency key; None when already taken."""
        item = {
            "idempotency_key": key,
            "customer_id": customer_id,
            "transaction_id": transaction_id,
            "status": "reserved",
            "response_payload": payload,
            "created_at": utcnow_iso(),
            "expires_at": int(time.time()) + IDEMPOTENCY_TTL_SECONDS,
        }
        try:
            self.idempotency.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(idempotency_key)",
            )
        except self.conditional_failed:
            return None
        return item

    def get_idempotency_record(self, key):
        """Fetch an idempotency record or None."""
        response = self.idempotency.get_item(Key={"idempotency_key": key})
        item = response.get("Item")
        return json_safe(item) if item else None

    def claim_idempotency_record(self, key):
        """Move a reserved key to 'processing'; False if already claimed/done."""
        try:
            self.idempotency.update_item(
                Key={"idempotency_key": key},
                UpdateExpression="SET #status = :processing, claimed_at = :now",
                ConditionExpression="attribute_exists(idempotency_key) AND #status = :reserved",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "processing",
                    ":reserved": "reserved",
                    ":now": utcnow_iso(),
                },
            )
        except self.conditional_failed:
            return False
        return True

    def complete_idempotency_record(self, key, status, payload):
        """Store the final status/result for an idempotency key."""
        self.idempotency.update_item(
            Key={"idempotency_key": key},
            UpdateExpression="SET #status = :status, response_payload = :payload, completed_at = :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": status,
                ":payload": payload,
                ":now": utcnow_iso(),
            },
        )

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------
    def enqueue_purchase(self, message):
        """Send a purchase message to SQS."""
        response = self.sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(json_safe(message), separators=(",", ":")),
        )
        return response.get("MessageId")

    def receive_purchase_messages(self, max_messages=10, wait_seconds=0):
        """Receive purchase messages from SQS."""
        response = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max(1, min(10, int(max_messages))),
            WaitTimeSeconds=int(wait_seconds),
        )
        messages = []
        for raw in response.get("Messages", []):
            try:
                body = json.loads(raw.get("Body") or "{}")
            except ValueError:
                body = {}
            messages.append({"receipt_handle": raw.get("ReceiptHandle"), "body": body})
        return messages

    def delete_purchase_message(self, receipt_handle):
        """Delete a processed message from SQS."""
        self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt_handle)

    # ------------------------------------------------------------------
    # audit log
    # ------------------------------------------------------------------
    def put_audit_entry(self, entry):
        """Append a JSON audit entry to S3 and return it (with its key)."""
        record = dict(entry)
        recorded_at = record.get("recorded_at") or utcnow_iso()
        record["recorded_at"] = recorded_at
        record.setdefault("audit_id", uuid.uuid4().hex)
        key = audit_key(record.get("customer_id", "unknown"), recorded_at, record.get("transaction_id", "na"))
        record["s3_key"] = key
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(json_safe(record), separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
        )
        return record

    def list_audit_entries(self, customer_id, limit=50):
        """List audit-log object metadata for a customer."""
        prefix = "customers/{0}/".format(customer_id)
        response = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=int(limit))
        entries = []
        for obj in response.get("Contents", []):
            modified = obj.get("LastModified")
            entries.append(
                {
                    "key": obj.get("Key"),
                    "size": int(obj.get("Size", 0)),
                    "last_modified": modified.isoformat() if modified is not None else None,
                }
            )
        entries.sort(key=lambda item: item["key"], reverse=True)
        return entries

    def get_audit_entry(self, key):
        """Fetch and parse a single audit-log object."""
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
        except self.s3.exceptions.NoSuchKey:
            return None
        try:
            return json.loads(response["Body"].read().decode("utf-8"))
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # notifications
    # ------------------------------------------------------------------
    def publish_tier_upgrade(self, notification):
        """Publish a gold-tier upgrade notification to SNS."""
        response = self.sns.publish(
            TopicArn=self.topic_arn,
            Subject="Loyalty tier upgrade",
            Message=json.dumps(json_safe(notification), separators=(",", ":")),
            MessageAttributes={
                "event_type": {"DataType": "String", "StringValue": "tier_upgrade"},
            },
        )
        return response.get("MessageId")

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------
    def _probe_dynamodb(self):
        client = self.dynamodb.meta.client
        for table_name in (
            self.customers_table_name,
            self.transactions_table_name,
            self.idempotency_table_name,
        ):
            client.describe_table(TableName=table_name)

    def _probe_sqs(self):
        self.sqs.get_queue_attributes(QueueUrl=self.queue_url, AttributeNames=["QueueArn"])

    def _probe_sns(self):
        self.sns.get_topic_attributes(TopicArn=self.topic_arn)

    def _probe_s3(self):
        self.s3.head_bucket(Bucket=self.bucket)

    def health(self):
        """Probe every dependency, returning 'ok' or an error label per service."""
        probes = (
            ("dynamodb", self._probe_dynamodb),
            ("sqs", self._probe_sqs),
            ("sns", self._probe_sns),
            ("s3", self._probe_s3),
        )
        checks = {}
        for name, probe in probes:
            try:
                probe()
                checks[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - health probe must never raise
                checks[name] = "error: {0}".format(exc.__class__.__name__)
        return checks


def build_repository():
    """Build the default environment-configured repository."""
    return LoyaltyRepository()
