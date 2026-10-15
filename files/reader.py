"""Safe reading of uploaded text files."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import FileLimits
from files.validation import (
    FileValidationError,
    basename,
    extension_of,
    is_allowed_filename,
    is_probably_binary,
    validate_size,
)
from utils import human_bytes

#: Encodings tried, in order, when decoding uploaded text.
DECODE_ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "cp1252", "latin-1")


@dataclass
class ReadFile:
    """A successfully read text file."""

    filename: str
    text: str
    encoding: str
    size_bytes: int
    extension: str

    @property
    def line_count(self) -> int:
        return self.text.count("\n") + (1 if self.text and not self.text.endswith("\n") else 0)


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Decode bytes into text, returning ``(text, encoding)``.

    Tries UTF-8 first and falls back to code pages that never raise.  Raises
    :class:`FileValidationError` when the data is clearly binary.
    """

    if is_probably_binary(data):
        raise FileValidationError(
            "That file looks like binary data and cannot be read as text.",
            code="binary_content",
        )
    for encoding in DECODE_ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    raise FileValidationError("The file could not be decoded as text.", code="decode_error")


def read_text_file(
    *,
    filename: str,
    data: bytes,
    limits: FileLimits,
    max_bytes: Optional[int] = None,
) -> ReadFile:
    """Validate and read a single uploaded text file."""

    name = basename(filename)
    if not name:
        raise FileValidationError("The attachment had no filename.", code="missing_name")
    limit = min(limits.max_attachment_bytes, max_bytes) if max_bytes else limits.max_attachment_bytes
    validate_size(len(data), limit, what="file")
    if not is_allowed_filename(name):
        raise FileValidationError(
            f"`{name}` is not a supported text or source file type.", code="unsupported_type"
        )
    text, encoding = decode_bytes(data)
    return ReadFile(
        filename=name,
        text=text,
        encoding=encoding,
        size_bytes=len(data),
        extension=extension_of(name),
    )


def read_text_file_or_none(
    *, filename: str, data: bytes, limits: FileLimits
) -> tuple[Optional[ReadFile], Optional[str]]:
    """Read a file, returning ``(result, skip_reason)`` instead of raising."""

    try:
        return read_text_file(filename=filename, data=data, limits=limits), None
    except FileValidationError as exc:
        return None, exc.user_message


def describe_file(read_file: ReadFile) -> str:
    """A short, safe one-line description for embedding in a prompt or reply."""

    return (
        f"`{read_file.filename}` - {human_bytes(read_file.size_bytes)}, "
        f"{read_file.extension or 'no extension'}, encoding {read_file.encoding}"
    )
