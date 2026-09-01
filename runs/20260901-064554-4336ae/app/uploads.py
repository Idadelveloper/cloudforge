"""Stdlib-only parsing of document upload request bodies.

Three body flavours are supported so the service has no extra dependencies:

* ``multipart/form-data`` - a single file part plus metadata fields.
* ``application/json``    - ``content_base64`` (or ``content``) plus metadata keys.
* anything else           - the raw body is the file, metadata comes from the query string.
"""

import base64
import binascii
import json
from typing import Any, Dict, List, Optional, Tuple


class UploadError(ValueError):
    """Raised when an upload body cannot be interpreted."""


class ParsedUpload:
    """Normalised representation of an upload request."""

    def __init__(self, data: bytes, filename: Optional[str], content_type: Optional[str],
                 fields: Optional[Dict[str, List[str]]] = None):
        self.data = data or b""
        self.filename = filename or "document.bin"
        self.content_type = content_type or "application/octet-stream"
        self.fields = fields or {}

    def field(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.fields.get(name) or []
        return values[0] if values else default

    def values(self, name: str) -> List[str]:
        return list(self.fields.get(name) or [])


def _split_params(value: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
            current.append(char)
        elif char == ";" and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def parse_header(value: Optional[str]) -> Tuple[str, Dict[str, str]]:
    """Split a header value into its main token and its parameters."""
    parts = _split_params(value or "")
    if not parts:
        return "", {}
    params: Dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            name, _, raw = part.partition("=")
            params[name.strip().lower()] = raw.strip().strip('"')
    return parts[0].strip().lower(), params


def _strip_edges(chunk: bytes) -> bytes:
    if chunk.startswith(b"\r\n"):
        chunk = chunk[2:]
    elif chunk.startswith(b"\n"):
        chunk = chunk[1:]
    if chunk.endswith(b"\r\n"):
        chunk = chunk[:-2]
    elif chunk.endswith(b"\n"):
        chunk = chunk[:-1]
    return chunk


def _split_part(segment: bytes) -> Tuple[Dict[str, str], bytes]:
    header_blob, sep, content = segment.partition(b"\r\n\r\n")
    if not sep:
        header_blob, sep, content = segment.partition(b"\n\n")
    if not sep:
        return {}, segment
    headers: Dict[str, str] = {}
    for line in header_blob.replace(b"\r\n", b"\n").split(b"\n"):
        text = line.decode("utf-8", "replace")
        if ":" in text:
            name, _, value = text.partition(":")
            headers[name.strip().lower()] = value.strip()
    return headers, content


def parse_multipart(content_type: str, body: bytes) -> Tuple[Dict[str, List[str]], List[Dict[str, Any]]]:
    """Parse a multipart/form-data body into (fields, files)."""
    _, params = parse_header(content_type)
    boundary = params.get("boundary")
    if not boundary:
        raise UploadError("multipart request is missing a boundary parameter")
    delimiter = b"--" + boundary.encode("utf-8")
    fields: Dict[str, List[str]] = {}
    files: List[Dict[str, Any]] = []
    for chunk in (body or b"").split(delimiter)[1:]:
        if chunk[:2] == b"--":
            break
        headers, content = _split_part(_strip_edges(chunk))
        _, disposition = parse_header(headers.get("content-disposition", ""))
        name = disposition.get("name") or ""
        filename = disposition.get("filename")
        if filename is not None:
            files.append(
                {
                    "name": name,
                    "filename": filename,
                    "content_type": headers.get("content-type") or "application/octet-stream",
                    "data": content,
                }
            )
        else:
            fields.setdefault(name, []).append(content.decode("utf-8", "replace"))
    return fields, files


def _merge_query(fields: Dict[str, List[str]], query: Optional[Dict[str, List[str]]]) -> Dict[str, List[str]]:
    merged = {key: [str(item) for item in values] for key, values in (fields or {}).items()}
    for key, values in (query or {}).items():
        if key not in merged:
            merged[key] = [str(item) for item in values]
    return merged


def _decode_json_content(payload: Dict[str, Any]) -> bytes:
    if "content_base64" in payload:
        raw = payload.get("content_base64") or ""
        try:
            return base64.b64decode(str(raw), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UploadError("'content_base64' is not valid base64") from exc
    if "content" in payload:
        return str(payload.get("content") or "").encode("utf-8")
    raise UploadError("JSON upload requires 'content_base64' or 'content'")


def parse_upload(content_type: Optional[str], body: bytes,
                 query: Optional[Dict[str, List[str]]] = None) -> ParsedUpload:
    """Turn an HTTP request body into a :class:`ParsedUpload`."""
    mime, _ = parse_header(content_type or "")
    body = body or b""

    if mime.startswith("multipart/"):
        fields, files = parse_multipart(content_type or "", body)
        chosen = None
        for entry in files:
            if entry["name"] == "file":
                chosen = entry
                break
        if chosen is None and files:
            chosen = files[0]
        if chosen is None:
            raise UploadError("multipart upload must include a file part")
        merged = _merge_query(fields, query)
        return ParsedUpload(chosen["data"], chosen["filename"], chosen["content_type"], merged)

    if mime == "application/json" or mime.endswith("+json"):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise UploadError("request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise UploadError("JSON body must be an object")
        data = _decode_json_content(payload)
        fields: Dict[str, List[str]] = {}
        for key, value in payload.items():
            if key in ("content", "content_base64"):
                continue
            if isinstance(value, (list, tuple)):
                fields[key] = [str(item) for item in value]
            else:
                fields[key] = [str(value)]
        merged = _merge_query(fields, query)
        filename = merged.get("filename", [None])[0]
        item_type = merged.get("content_type", ["application/octet-stream"])[0]
        return ParsedUpload(data, filename, item_type, merged)

    merged = _merge_query({}, query)
    filename = merged.get("filename", [None])[0]
    item_type = merged.get("content_type", [mime or "application/octet-stream"])[0]
    return ParsedUpload(body, filename, item_type, merged)
