"""Stdlib helpers for reading uploaded image payloads.

The service accepts either a ``multipart/form-data`` body (parsed here with the
standard library ``email`` package) or a raw binary body, which keeps the
dependency surface limited to the approved third-party packages.
"""

import email
import email.policy
import os
import re
from dataclasses import dataclass
from typing import Optional, Tuple

MAX_FILENAME_LENGTH = 120
DEFAULT_FILENAME = "upload.bin"
DEFAULT_CONTENT_TYPE = "application/octet-stream"


@dataclass
class Upload:
    """A single uploaded file."""

    filename: str
    content_type: str
    data: bytes


def sanitize_filename(name: Optional[str]) -> str:
    """Return a safe, S3 friendly file name."""
    base = os.path.basename((name or "").strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    if not cleaned:
        return DEFAULT_FILENAME
    return cleaned[:MAX_FILENAME_LENGTH]


def _first_file_part(content_type: str, body: bytes) -> Optional[Tuple[Optional[str], str, bytes]]:
    """Return (filename, content_type, data) of the first useful multipart part."""
    raw = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
    message = email.message_from_bytes(raw, policy=email.policy.default)
    if not message.is_multipart():
        return None
    fallback: Optional[Tuple[Optional[str], str, bytes]] = None
    for part in message.iter_parts():
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        part_type = part.get_content_type() or DEFAULT_CONTENT_TYPE
        if filename:
            return filename, part_type, payload
        if fallback is None and payload:
            fallback = (None, part_type, payload)
    return fallback


def parse_upload(content_type: str, body: bytes, fallback_filename: Optional[str] = None) -> Upload:
    """Extract a single uploaded file from a request body.

    Raises:
        ValueError: when a multipart body contains no usable file part.
    """
    normalized = (content_type or "").lower()
    if normalized.startswith("multipart/form-data"):
        part = _first_file_part(content_type, body)
        if part is None:
            raise ValueError("multipart body did not contain a file part")
        filename, part_type, data = part
        return Upload(
            filename=sanitize_filename(filename or fallback_filename),
            content_type=part_type or DEFAULT_CONTENT_TYPE,
            data=data,
        )
    return Upload(
        filename=sanitize_filename(fallback_filename),
        content_type=content_type or DEFAULT_CONTENT_TYPE,
        data=body,
    )
