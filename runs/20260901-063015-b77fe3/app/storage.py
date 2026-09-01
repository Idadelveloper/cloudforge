"""Data access layer: DynamoDB persistence and SNS notification for feedback.

Everything that touches AWS lives behind the small ``FeedbackRepository`` and
``Notifier`` interfaces, so the HTTP layer (and the tests) can swap in
in-memory implementations without any network access.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

LOGGER = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE_NAME = "product-feedback"
DEFAULT_INDEX_NAME = "product_id-created_at-index"
DEFAULT_TOPIC_NAME = "product-feedback-low-rating-alerts"
LOW_RATING_THRESHOLD = 2
MAX_SCAN_PAGES = 20


class StorageError(RuntimeError):
    """Raised when the backing data store cannot be reached."""


def aws_region() -> str:
    """Resolve the AWS region from the environment."""
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )


def aws_endpoint_url() -> Optional[str]:
    """Return the LocalStack/custom endpoint if configured."""
    return os.environ.get("AWS_ENDPOINT_URL") or None


def dynamodb_resource():
    """Create a DynamoDB resource honouring AWS_ENDPOINT_URL."""
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


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_item(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce a raw (possibly DynamoDB Decimal-typed) item into plain JSON types."""
    data = dict(item or {})
    return {
        "feedback_id": str(data.get("feedback_id", "")),
        "product_id": str(data.get("product_id", "")),
        "rating": _to_int(data.get("rating")),
        "comment": str(data.get("comment", "")),
        "customer_email": data.get("customer_email") or None,
        "created_at": str(data.get("created_at", "")),
        "alert_sent": bool(data.get("alert_sent", False)),
    }


def apply_filters(
    items: List[Dict[str, Any]],
    product_id: Optional[str] = None,
    rating: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Normalize, filter and sort (newest first) a list of raw items."""
    result = [normalize_item(item) for item in items]
    if product_id:
        result = [item for item in result if item["product_id"] == product_id]
    if rating is not None:
        result = [item for item in result if item["rating"] == int(rating)]
    result.sort(key=lambda item: item["created_at"], reverse=True)
    if limit is not None and limit >= 0:
        result = result[:limit]
    return result


class FeedbackRepository:
    """Interface for feedback persistence."""

    def save(self, item: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def get(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def list_feedback(
        self,
        product_id: Optional[str] = None,
        rating: Optional[int] = None,
        limit: Optional[int] = 50,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


class Notifier:
    """Interface for low-rating notifications."""

    def publish_low_rating(self, alert: Dict[str, Any]) -> bool:
        raise NotImplementedError


class DynamoFeedbackRepository(FeedbackRepository):
    """DynamoDB backed feedback repository."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        index_name: Optional[str] = None,
        resource: Any = None,
    ) -> None:
        self.table_name = table_name or os.environ.get("FEEDBACK_TABLE", DEFAULT_TABLE_NAME)
        self.index_name = index_name or os.environ.get(
            "FEEDBACK_PRODUCT_INDEX", DEFAULT_INDEX_NAME
        )
        self._resource = resource

    def _table(self):
        if self._resource is None:
            self._resource = dynamodb_resource()
        return self._resource.Table(self.table_name)

    def save(self, item: Dict[str, Any]) -> Dict[str, Any]:
        payload = {key: value for key, value in item.items() if value is not None}
        try:
            self._table().put_item(Item=payload)
        except Exception as exc:  # noqa: BLE001 - surfaced as StorageError
            raise StorageError("failed to store feedback: {0}".format(exc)) from exc
        return normalize_item(item)

    def get(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self._table().get_item(Key={"feedback_id": feedback_id})
        except Exception as exc:  # noqa: BLE001
            raise StorageError("failed to read feedback: {0}".format(exc)) from exc
        item = response.get("Item")
        return normalize_item(item) if item else None

    def list_feedback(
        self,
        product_id: Optional[str] = None,
        rating: Optional[int] = None,
        limit: Optional[int] = 50,
    ) -> List[Dict[str, Any]]:
        raw = self._query_product(product_id) if product_id else self._scan_all()
        return apply_filters(raw, product_id=product_id, rating=rating, limit=limit)

    def _scan_all(self) -> List[Dict[str, Any]]:
        table = self._table()
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {}
        pages = 0
        try:
            while pages < MAX_SCAN_PAGES:
                response = table.scan(**kwargs)
                items.extend(response.get("Items", []))
                pages += 1
                start_key = response.get("LastEvaluatedKey")
                if not start_key:
                    break
                kwargs = {"ExclusiveStartKey": start_key}
        except Exception as exc:  # noqa: BLE001
            raise StorageError("failed to list feedback: {0}".format(exc)) from exc
        return items

    def _query_product(self, product_id: str) -> List[Dict[str, Any]]:
        try:
            response = self._table().query(
                IndexName=self.index_name,
                KeyConditionExpression=Key("product_id").eq(product_id),
            )
            return list(response.get("Items", []))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("GSI query failed (%s); falling back to table scan", exc)
        return self._scan_all()


class SnsNotifier(Notifier):
    """Publishes low-rating alerts to an SNS topic."""

    def __init__(
        self,
        topic_arn: Optional[str] = None,
        topic_name: Optional[str] = None,
        client: Any = None,
    ) -> None:
        self.topic_arn = topic_arn or os.environ.get("LOW_RATING_TOPIC_ARN", "")
        self.topic_name = topic_name or os.environ.get(
            "LOW_RATING_TOPIC_NAME", DEFAULT_TOPIC_NAME
        )
        self._client = client

    def _sns(self):
        if self._client is None:
            self._client = sns_client()
        return self._client

    def _resolve_topic_arn(self) -> str:
        if self.topic_arn:
            return self.topic_arn
        response = self._sns().create_topic(Name=self.topic_name)
        self.topic_arn = response.get("TopicArn", "")
        return self.topic_arn

    def publish_low_rating(self, alert: Dict[str, Any]) -> bool:
        subject = "Low rating {0} for product {1}".format(
            alert.get("rating"), alert.get("product_id")
        )[:100]
        try:
            topic_arn = self._resolve_topic_arn()
            if not topic_arn:
                LOGGER.warning("no SNS topic configured; skipping low-rating alert")
                return False
            self._sns().publish(
                TopicArn=topic_arn,
                Subject=subject,
                Message=json.dumps(alert),
            )
            LOGGER.info("published low-rating alert for %s", alert.get("feedback_id"))
            return True
        except Exception as exc:  # noqa: BLE001 - alerting must not break writes
            LOGGER.error("failed to publish low-rating alert: %s", exc)
            return False


class InMemoryFeedbackRepository(FeedbackRepository):
    """In-memory repository used for local runs and tests."""

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        for item in items or []:
            normalized = normalize_item(item)
            self._items[normalized["feedback_id"]] = normalized

    def save(self, item: Dict[str, Any]) -> Dict[str, Any]:
        normalized = normalize_item(item)
        self._items[normalized["feedback_id"]] = normalized
        return dict(normalized)

    def get(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(feedback_id)
        return dict(item) if item else None

    def list_feedback(
        self,
        product_id: Optional[str] = None,
        rating: Optional[int] = None,
        limit: Optional[int] = 50,
    ) -> List[Dict[str, Any]]:
        return apply_filters(
            list(self._items.values()),
            product_id=product_id,
            rating=rating,
            limit=limit,
        )


class RecordingNotifier(Notifier):
    """Notifier that records alerts in memory (used for tests/local runs)."""

    def __init__(self, succeed: bool = True) -> None:
        self.succeed = succeed
        self.messages: List[Dict[str, Any]] = []

    def publish_low_rating(self, alert: Dict[str, Any]) -> bool:
        self.messages.append(dict(alert))
        return self.succeed


class FeedbackService:
    """Application logic tying the repository and notifier together."""

    def __init__(
        self,
        repository: FeedbackRepository,
        notifier: Optional[Notifier] = None,
        threshold: int = LOW_RATING_THRESHOLD,
    ) -> None:
        self.repository = repository
        self.notifier = notifier
        self.threshold = threshold

    def create_feedback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        item = {
            "feedback_id": str(uuid.uuid4()),
            "product_id": str(payload["product_id"]),
            "rating": int(payload["rating"]),
            "comment": str(payload["comment"]),
            "customer_email": payload.get("customer_email") or None,
            "created_at": utc_now_iso(),
            "alert_sent": False,
        }
        if item["rating"] <= self.threshold and self.notifier is not None:
            alert = {
                "feedback_id": item["feedback_id"],
                "product_id": item["product_id"],
                "rating": item["rating"],
                "comment": item["comment"],
                "created_at": item["created_at"],
            }
            item["alert_sent"] = bool(self.notifier.publish_low_rating(alert))
        self.repository.save(item)
        return normalize_item(item)

    def get_feedback(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        return self.repository.get(feedback_id)

    def list_feedback(
        self,
        product_id: Optional[str] = None,
        rating: Optional[int] = None,
        limit: Optional[int] = 50,
    ) -> List[Dict[str, Any]]:
        return self.repository.list_feedback(
            product_id=product_id, rating=rating, limit=limit
        )

    def average_rating(self, product_id: Optional[str] = None) -> Dict[str, Any]:
        items = self.repository.list_feedback(
            product_id=product_id, rating=None, limit=None
        )
        breakdown = {str(value): 0 for value in range(1, 6)}
        total = 0
        for item in items:
            rating = item["rating"]
            total += rating
            key = str(rating)
            if key in breakdown:
                breakdown[key] += 1
        count = len(items)
        average = round(total / count, 2) if count else 0.0
        return {
            "product_id": product_id,
            "average_rating": average,
            "count": count,
            "rating_breakdown": breakdown,
        }
