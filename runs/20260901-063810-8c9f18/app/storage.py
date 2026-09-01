"""Data access layer: DynamoDB repository and SNS notifier for the feedback service."""
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

LOGGER = logging.getLogger("product_feedback_service.storage")

DEFAULT_TABLE_NAME = "product-feedback"
DEFAULT_TOPIC_NAME = "low-rating-alerts"
DEFAULT_PRODUCT_INDEX = "product_id-created_at-index"
DEFAULT_PAGE_LIMIT = 500


def aws_region() -> str:
    """Return the configured AWS region (defaults to us-east-1)."""
    return os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def aws_endpoint_url() -> Optional[str]:
    """Return the AWS endpoint override (LocalStack) or None."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource():
    """Create a DynamoDB service resource honouring AWS_ENDPOINT_URL."""
    return boto3.resource(
        "dynamodb",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def sns_client():
    """Create an SNS client honouring AWS_ENDPOINT_URL."""
    return boto3.client(
        "sns",
        region_name=aws_region(),
        endpoint_url=aws_endpoint_url(),
    )


def table_name() -> str:
    """Name of the DynamoDB table holding feedback records."""
    return os.environ.get("FEEDBACK_TABLE_NAME", DEFAULT_TABLE_NAME)


def topic_name() -> str:
    """Name of the SNS topic used for low-rating alerts."""
    return os.environ.get("FEEDBACK_TOPIC_NAME", DEFAULT_TOPIC_NAME)


def decode_value(value: Any) -> Any:
    """Convert DynamoDB Decimal values into plain ints/floats, recursively."""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: decode_value(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [decode_value(inner) for inner in value]
    return value


def decode_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a raw DynamoDB item into a plain JSON-friendly dict."""
    decoded = {key: decode_value(value) for key, value in (item or {}).items()}
    decoded.setdefault("product_id", "general")
    decoded.setdefault("comment", "")
    decoded.setdefault("created_at", "")
    decoded.setdefault("alert_sent", False)
    decoded.setdefault("customer_email", None)
    return decoded


class DynamoFeedbackRepository:
    """Feedback persistence backed by a DynamoDB table."""

    def __init__(self, table: Any = None, index_name: Optional[str] = None, page_limit: Optional[int] = None) -> None:
        self._table = table
        self._index_name = index_name or os.environ.get("FEEDBACK_PRODUCT_INDEX", DEFAULT_PRODUCT_INDEX)
        try:
            self._page_limit = int(page_limit or os.environ.get("FEEDBACK_PAGE_LIMIT", DEFAULT_PAGE_LIMIT))
        except (TypeError, ValueError):
            self._page_limit = DEFAULT_PAGE_LIMIT

    @property
    def table(self) -> Any:
        """Lazily create the boto3 Table resource."""
        if self._table is None:
            self._table = dynamodb_resource().Table(table_name())
        return self._table

    def put_feedback(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Write (or overwrite) a feedback item."""
        stored = {key: value for key, value in item.items() if value is not None}
        self.table.put_item(Item=stored)
        return item

    def get_feedback(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single feedback record, or None when missing."""
        response = self.table.get_item(Key={"feedback_id": feedback_id})
        item = (response or {}).get("Item")
        if not item:
            return None
        return decode_item(item)

    def all_feedback(self, product_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return a bounded set of feedback records, newest first."""
        items = self._fetch(product_id)
        return sorted(items, key=lambda entry: str(entry.get("created_at", "")), reverse=True)

    def list_feedback(
        self,
        product_id: Optional[str] = None,
        min_rating: Optional[int] = None,
        max_rating: Optional[int] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return feedback records filtered by product and rating range."""
        items = self.all_feedback(product_id)
        if min_rating is not None:
            items = [entry for entry in items if int(entry.get("rating", 0)) >= int(min_rating)]
        if max_rating is not None:
            items = [entry for entry in items if int(entry.get("rating", 0)) <= int(max_rating)]
        if limit and limit > 0:
            items = items[:limit]
        return items

    def ping(self) -> bool:
        """Return True when the DynamoDB table is reachable."""
        try:
            return bool(self.table.table_status)
        except Exception as exc:  # pragma: no cover - depends on AWS availability
            LOGGER.warning("DynamoDB table unreachable: %s", exc)
            return False

    def _fetch(self, product_id: Optional[str]) -> List[Dict[str, Any]]:
        if product_id:
            try:
                response = self.table.query(
                    IndexName=self._index_name,
                    KeyConditionExpression=Key("product_id").eq(product_id),
                    ScanIndexForward=False,
                    Limit=self._page_limit,
                )
                return [decode_item(raw) for raw in (response or {}).get("Items", [])]
            except Exception as exc:
                LOGGER.warning("GSI query failed (%s); falling back to table scan", exc)

        response = self.table.scan(Limit=self._page_limit)
        items = [decode_item(raw) for raw in (response or {}).get("Items", [])]
        if product_id:
            items = [entry for entry in items if entry.get("product_id") == product_id]
        return items


class SnsNotifier:
    """Publishes low-rating alerts to an SNS topic."""

    def __init__(self, client: Any = None, topic_arn: Optional[str] = None) -> None:
        self._client = client
        self._topic_arn = topic_arn or os.environ.get("SNS_TOPIC_ARN") or None

    @property
    def client(self) -> Any:
        """Lazily create the boto3 SNS client."""
        if self._client is None:
            self._client = sns_client()
        return self._client

    def resolve_topic_arn(self) -> str:
        """Return the topic ARN, resolving it by name when not configured."""
        if self._topic_arn:
            return self._topic_arn
        response = self.client.create_topic(Name=topic_name())
        self._topic_arn = (response or {}).get("TopicArn", "")
        return self._topic_arn

    def publish_low_rating(self, alert: Dict[str, Any]) -> bool:
        """Publish a low-rating alert; returns True on success, False on failure."""
        subject = "Low rating alert: {0} star(s) for {1}".format(
            alert.get("rating"), alert.get("product_id", "general")
        )[:99]
        try:
            arn = self.resolve_topic_arn()
            if not arn:
                LOGGER.error("No SNS topic ARN available; alert not published")
                return False
            self.client.publish(TopicArn=arn, Subject=subject, Message=json.dumps(alert))
            LOGGER.info("Published low rating alert for %s", alert.get("feedback_id"))
            return True
        except Exception as exc:
            LOGGER.error("Failed to publish low rating alert: %s", exc)
            return False

    def ping(self) -> bool:
        """Return True when the SNS topic is reachable."""
        try:
            arn = self.resolve_topic_arn()
            if not arn:
                return False
            self.client.get_topic_attributes(TopicArn=arn)
            return True
        except Exception as exc:  # pragma: no cover - depends on AWS availability
            LOGGER.warning("SNS topic unreachable: %s", exc)
            return False
