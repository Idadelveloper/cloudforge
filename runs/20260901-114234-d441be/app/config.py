"""Runtime configuration: the shared API key, optionally from Secrets Manager."""
import json
import logging
import os
from typing import Dict, Optional

import boto3

LOGGER = logging.getLogger(__name__)

_SECRET_CACHE: Dict[str, Optional[str]] = {}


def region_name() -> str:
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


def endpoint_url() -> Optional[str]:
    return os.environ.get("AWS_ENDPOINT_URL") or None


def secretsmanager_client():
    return boto3.client("secretsmanager", region_name=region_name(), endpoint_url=endpoint_url())


def _load_secret_api_key(secret_name: str) -> Optional[str]:
    try:
        response = secretsmanager_client().get_secret_value(SecretId=secret_name)
    except Exception as exc:  # noqa: BLE001 - config is optional
        LOGGER.warning("could not read secret %s: %s", secret_name, exc)
        return None
    raw = response.get("SecretString")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw.strip() or None
    if isinstance(parsed, dict):
        for field in ("api_key", "apiKey", "API_KEY"):
            value = parsed.get(field)
            if value:
                return str(value)
        return None
    return str(parsed)


def get_api_key() -> Optional[str]:
    """Return the configured API key, or None when auth is disabled."""
    direct = os.environ.get("LOYALTY_API_KEY")
    if direct:
        return direct
    secret_name = os.environ.get("LOYALTY_SECRET_NAME")
    if not secret_name:
        return None
    if secret_name not in _SECRET_CACHE:
        _SECRET_CACHE[secret_name] = _load_secret_api_key(secret_name)
    return _SECRET_CACHE[secret_name]


def reset_cache() -> None:
    _SECRET_CACHE.clear()
