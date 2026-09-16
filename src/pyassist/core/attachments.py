"""File upload: turn local files into message attachments.

Strategy
--------
* **Text files** are read inline. If the user's system prompt says the
  assistant must respond with the file's *full* content verbatim, inline text
  lets the model produce a byte-perfect copy.
* **Images** become ``data:`` URLs when vision is enabled. This works with any
  OpenAI-compatible multimodal endpoint without needing an upload API.
* **Binary/oversized files** are *not* embedded; we attach a warning that a
  tool can read the file directly instead.

Limits come from the config (``max_attachment_bytes``), so users can raise
them for their local server.
"""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Callable
from pathlib import Path

from .models import Attachment

_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}

#: MIME prefixes whose contents are safe to inline as text.
_TEXT_PREFIXES = ("text/", "application/json", "application/xml", "application/javascript")
_UTF16_SIGNATURES = (b"\xff\xfe", b"\xfe\xff")

_MISSING = object()


def _default_read_text(path: Path) -> str | object:
    """Decode a file as text, tolerating legacy encodings."""
    raw = path.read_bytes()
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        if raw.startswith(_UTF16_SIGNATURES):
            try:
                return raw.decode("utf-16")
            except UnicodeDecodeError:
                return _MISSING
        # ISO-8859-1 decodes any byte sequence - a safe last resort for
        # legacy encodings that never produces a crash.
        return raw.decode("latin-1")


def build_attachment(
    path: str | Path,
    *,
    max_bytes: int = 256 * 1024,
    vision_enabled: bool = True,
    read_text: Callable[[Path], str | object] = _default_read_text,
) -> Attachment:
    """Classify and inline a file. Raises :class:`OSError` for unreadable files."""
    file_path = Path(path).expanduser()
    stat = file_path.stat()
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    name = file_path.name
    base = Attachment(path=str(file_path), name=name, mime_type=mime, size_bytes=stat.st_size)

    if stat.st_size > max_bytes:
        base.truncated = True
        base.content = ""
        return base

    if mime in _IMAGE_MIMES and vision_enabled:
        base.kind = "image"
        data = file_path.read_bytes()
        encoded = base64.b64encode(data).decode("ascii")
        base.content = f"data:{mime};base64,{encoded}"
        base.truncated = len(encoded) > max_bytes
        return base

    if _is_text(mime, file_path):
        base.kind = "text"
        content = read_text(file_path)
        if content is _MISSING:
            base.truncated = True
            base.content = ""
            return base
        base.content = content
        base.truncated = len(content) > max_bytes
        return base

    # Binary or unsupported: nothing to inline.
    base.kind = "text"
    base.content = ""
    return base


def _is_text(mime: str, path: Path) -> bool:
    if mime.startswith(_TEXT_PREFIXES) or mime in {"application/x-httpd-php", "text/markdown"}:
        return True
    # Extension-based fallback for files with unknown MIME types.
    return path.suffix.lower() in {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".txt", ".json", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".sh", ".bat", ".ps1", ".csv", ".html", ".css",
        ".rs", ".go", ".java", ".c", ".cpp", ".h", ".sql", ".xml", ".dockerfile",
    }
