"""Minimal API Gateway (AWS_PROXY) to ASGI adapter for the FastAPI app.

Only the standard library is used: the Lambda event is translated into an ASGI
scope, the application is invoked with ``asyncio.run`` and the collected ASGI
messages are turned back into an API Gateway proxy response.
"""
import asyncio
import base64
from typing import Any, Dict, List, Tuple
from urllib.parse import urlencode

from app import app


def _method(event: Dict[str, Any]) -> str:
    method = event.get("httpMethod")
    if not method:
        context = event.get("requestContext") or {}
        method = (context.get("http") or {}).get("method")
    return str(method or "GET").upper()


def _path(event: Dict[str, Any]) -> str:
    return str(event.get("path") or event.get("rawPath") or "/")


def _body(event: Dict[str, Any]) -> bytes:
    raw = event.get("body")
    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw)
    return str(raw).encode("utf-8")


def _query_string(event: Dict[str, Any]) -> bytes:
    pairs: List[Tuple[str, str]] = []
    multi = event.get("multiValueQueryStringParameters")
    if multi:
        for key, values in multi.items():
            for value in values or []:
                pairs.append((str(key), str(value)))
    else:
        for key, value in (event.get("queryStringParameters") or {}).items():
            if value is not None:
                pairs.append((str(key), str(value)))
    return urlencode(pairs).encode("utf-8")


def _headers(event: Dict[str, Any]) -> List[Tuple[bytes, bytes]]:
    headers = event.get("headers") or {}
    return [
        (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
        for key, value in headers.items()
        if value is not None
    ]


def _build_scope(event: Dict[str, Any], body: bytes) -> Dict[str, Any]:
    path = _path(event)
    headers = _headers(event)
    if body and not any(key == b"content-length" for key, _ in headers):
        headers.append((b"content-length", str(len(body)).encode("latin-1")))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": _method(event),
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "root_path": "",
        "query_string": _query_string(event),
        "headers": headers,
        "client": ("127.0.0.1", 0),
        "server": ("lambda", 443),
    }


async def _run_app(scope: Dict[str, Any], body: bytes) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    sent = {"done": False}

    async def receive() -> Dict[str, Any]:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    return messages


def _to_proxy_response(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    status = 500
    headers: Dict[str, str] = {}
    multi_headers: Dict[str, List[str]] = {}
    chunks: List[bytes] = []
    for message in messages:
        if message.get("type") == "http.response.start":
            status = int(message.get("status", 500))
            for raw_key, raw_value in message.get("headers") or []:
                key = raw_key.decode("latin-1").lower()
                value = raw_value.decode("latin-1")
                headers[key] = value
                multi_headers.setdefault(key, []).append(value)
        elif message.get("type") == "http.response.body":
            chunks.append(message.get("body") or b"")
    payload = b"".join(chunks)
    response: Dict[str, Any] = {
        "statusCode": status,
        "headers": headers,
        "multiValueHeaders": multi_headers,
        "isBase64Encoded": False,
        "body": "",
    }
    try:
        response["body"] = payload.decode("utf-8")
    except UnicodeDecodeError:
        response["body"] = base64.b64encode(payload).decode("ascii")
        response["isBase64Encoded"] = True
    return response


def lambda_handler(event: Dict[str, Any], _context: Any = None) -> Dict[str, Any]:
    """AWS Lambda entrypoint for API Gateway proxy events."""
    body = _body(event)
    scope = _build_scope(event, body)
    messages = asyncio.run(_run_app(scope, body))
    return _to_proxy_response(messages)
