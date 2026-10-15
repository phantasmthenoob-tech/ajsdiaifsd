"""Small, dependency-free helpers shared across modules."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional

_MENTION_RE = re.compile(r"^<[@#]!?(\d{5,25})>$")


def utcnow_iso() -> str:
    """Return the current UTC time as a second-precision ISO-8601 string."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_snowflake(value: Any) -> Optional[int]:
    """Parse a Discord snowflake from an int, numeric string or mention.

    Accepts ``123456789012345678``, ``"<@1234...>"`` and ``"<#1234...>"``.
    Returns ``None`` when the value is not a Discord ID.
    """

    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; never a snowflake
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    match = _MENTION_RE.match(text)
    if match:
        text = match.group(1)
    if text.isdigit():
        parsed = int(text)
        return parsed if parsed > 0 else None
    return None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def human_bytes(size: int) -> str:
    """Format a byte count for humans (e.g. ``1.5 MB``)."""

    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def truncate_text(text: str, limit: int, *, marker: str = "\n…[truncated]") -> str:
    """Truncate text to ``limit`` characters, appending a marker when cut."""

    if limit <= 0 or len(text) <= limit:
        return text
    keep = max(0, limit - len(marker))
    return text[:keep] + marker


def chunk_text(text: str, limit: int) -> list[str]:
    """Split text into chunks no longer than ``limit`` characters.

    Splits on newlines when possible to keep code blocks and paragraphs intact,
    and hard-splits any single line that is itself too long.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            # A single line longer than the limit must be hard-split.
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [""]
