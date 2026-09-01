"""Helpers for reading uploaded files out of raw HTTP request bodies.

FastAPI's ``UploadFile`` support depends on the third-party ``python-multipart``
package, which is not part of the allowed dependency set, so multipart bodies
are parsed here with the standard library ``email`` package instead.  Raw
binary bodies (``application/octet-stream``, ``image/png``, ...) are also
accepted.
"""
from email.parser import BytesParser
from typing import Optional, Tuple

DEFAULT_FILENAME = "upload.bin"
DEFAULT_CONTENT_TYPE = "application/octet-stream"


def parse_multipart(content_type: str, body: bytes) -> Optional[Tuple[str, str, bytes]]:
    """Extract the first file part of a multipart/form-data body.

    Returns ``(filename, content_type, data)`` or ``None`` when no file part is
    present.
    """
    header = b"MIME-Version: 1.0\r\nContent-Type: "
    raw = header + content_type.encode("utf-8", "replace") + b"\r\n\r\n" + body
    message = BytesParser().parsebytes(raw)
    if not message.is_multipart():
        return None
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = part.get("Content-Disposition", "")
        if "filename" not in disposition and part.get_param("name", header="content-disposition") != "file":
            continue
        data = part.get_payload(decode=True)
        if data is None:
            continue
        filename = part.get_filename() or DEFAULT_FILENAME
        part_type = part.get("Content-Type") or DEFAULT_CONTENT_TYPE
        part_type = part_type.split(";")[0].strip() or DEFAULT_CONTENT_TYPE
        return filename, part_type, data
    return None


def parse_upload(content_type: str, body: bytes) -> Optional[Tuple[str, str, bytes]]:
    """Return ``(filename, content_type, data)`` for a multipart or raw upload."""
    normalised = (content_type or "").lower()
    if normalised.startswith("multipart/form-data"):
        return parse_multipart(content_type, body)
    plain_type = (content_type or DEFAULT_CONTENT_TYPE).split(";")[0].strip()
    return DEFAULT_FILENAME, plain_type or DEFAULT_CONTENT_TYPE, body
