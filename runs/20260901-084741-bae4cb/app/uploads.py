"""Minimal stdlib multipart/form-data parsing helpers.

Parsing multipart bodies here keeps the service free of extra third-party
dependencies while still accepting the ``multipart/form-data`` uploads described
in the specification.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional

DEFAULT_CONTENT_TYPE = "application/octet-stream"


@dataclass
class UploadedFile:
    filename: str
    content_type: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass
class ParsedForm:
    fields: Dict[str, str] = field(default_factory=dict)
    files: Dict[str, UploadedFile] = field(default_factory=dict)


def get_boundary(content_type: str) -> Optional[str]:
    """Extract the boundary token from a multipart content-type header."""
    if not content_type:
        return None
    parts = [segment.strip() for segment in content_type.split(";")]
    if not parts or not parts[0].lower().startswith("multipart/form-data"):
        return None
    for segment in parts[1:]:
        if segment.lower().startswith("boundary="):
            value = segment[len("boundary="):].strip()
            return value.strip('"') or None
    return None


def _parse_headers(blob: bytes) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for line in blob.split(b"\r\n"):
        if not line.strip():
            continue
        text = line.decode("utf-8", "replace")
        if ":" not in text:
            continue
        name, _, value = text.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


def _disposition_param(disposition: str, name: str) -> Optional[str]:
    for segment in disposition.split(";"):
        cleaned = segment.strip()
        prefix = name + "="
        if cleaned.lower().startswith(prefix):
            return cleaned[len(prefix):].strip().strip('"')
    return None


def parse_multipart_form(body: bytes, content_type: str) -> ParsedForm:
    """Parse a multipart/form-data body into simple fields and files."""
    boundary = get_boundary(content_type)
    if boundary is None:
        raise ValueError("content type is not multipart/form-data with a boundary")
    delimiter = b"--" + boundary.encode("utf-8")
    form = ParsedForm()
    segments = body.split(delimiter)
    for segment in segments[1:]:
        if segment.startswith(b"--"):
            break
        payload_segment = segment.lstrip(b"\r\n")
        if not payload_segment:
            continue
        header_blob, separator, payload = payload_segment.partition(b"\r\n\r\n")
        if not separator:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        headers = _parse_headers(header_blob)
        disposition = headers.get("content-disposition", "")
        name = _disposition_param(disposition, "name")
        if not name:
            continue
        filename = _disposition_param(disposition, "filename")
        if filename is not None:
            form.files[name] = UploadedFile(
                filename=filename or "document.bin",
                content_type=headers.get("content-type", DEFAULT_CONTENT_TYPE),
                content=payload,
            )
        else:
            form.fields[name] = payload.decode("utf-8", "replace")
    return form
