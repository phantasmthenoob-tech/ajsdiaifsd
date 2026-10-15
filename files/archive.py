"""Safe ZIP archive handling.

Reading an archive is treated as hostile input.  We enforce limits on the number
of entries, compressed and uncompressed size, nesting depth and per-entry
compression ratio, and we reject absolute paths, traversal, symlinks and
encrypted entries.  Nothing is ever executed or written to disk here; callers
receive an in-memory mapping of validated relative paths to bytes.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Mapping, Optional

from config import FileLimits
from files.validation import (
    FileValidationError,
    safe_project_path,
)
from utils import human_bytes


@dataclass
class ArchiveContents:
    """The validated, in-memory contents of an archive."""

    files: dict[str, bytes] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    total_uncompressed: int = 0
    file_count: int = 0


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix symlink mode bits live in the high 16 bits of external_attr.
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _depth_of(path: str) -> int:
    return path.count("/")


def read_zip(data: bytes, limits: FileLimits) -> ArchiveContents:
    """Read a ZIP archive safely into memory.

    Only supported text files are returned; binary or unsupported entries are
    skipped and reported, never silently dropped.
    """

    if len(data) > limits.max_archive_bytes:
        raise FileValidationError(
            f"The archive is too large ({human_bytes(len(data))}); the limit is "
            f"{human_bytes(limits.max_archive_bytes)}.",
            code="archive_too_large",
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise FileValidationError("That file is not a valid ZIP archive.", code="bad_zip") from exc

    contents = ArchiveContents()
    seen: set[str] = set()

    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_archive_files:
            raise FileValidationError(
                f"The archive contains {len(infos)} entries; the limit is "
                f"{limits.max_archive_files}.",
                code="too_many_files",
            )

        compressed_total = sum(max(i.compress_size, 1) for i in infos)
        declared_total = sum(i.file_size for i in infos)
        if declared_total > limits.max_archive_uncompressed_bytes:
            raise FileValidationError(
                "The archive would expand to more than the allowed "
                f"{human_bytes(limits.max_archive_uncompressed_bytes)}.",
                code="archive_bomb",
            )
        # Guard against grossly over-compressed archives (classic zip bombs).
        ratio = declared_total / max(compressed_total, 1)
        if ratio > limits.max_archive_compression_ratio:
            raise FileValidationError(
                "The archive has an unsafe compression ratio and was rejected.",
                code="archive_bomb",
            )

        running_total = 0
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                contents.skipped.append(f"{name} (encrypted)")
                continue

            try:
                rel = safe_project_path(name)
            except FileValidationError:
                # A traversal/absolute path invalidates the whole archive.
                raise
            if _is_symlink(info):
                raise FileValidationError(
                    "The archive contains symbolic links, which are not allowed.",
                    code="symlink",
                )
            if _depth_of(rel) > limits.max_archive_depth:
                contents.skipped.append(f"{rel} (too deep)")
                continue
            if rel in seen:
                contents.skipped.append(f"{rel} (duplicate entry)")
                continue
            if info.file_size > limits.max_archive_uncompressed_bytes:
                raise FileValidationError(
                    f"`{rel}` is too large to process.", code="member_too_large"
                )

            try:
                with archive.open(info) as handle:
                    raw = handle.read(limits.max_archive_uncompressed_bytes + 1)
            except (RuntimeError, zipfile.BadZipFile):
                contents.skipped.append(f"{rel} (unreadable)")
                continue

            if len(raw) > limits.max_archive_uncompressed_bytes:
                raise FileValidationError("An archive member is too large.", code="member_too_large")

            running_total += len(raw)
            if running_total > limits.max_archive_uncompressed_bytes:
                raise FileValidationError(
                    "The archive expands beyond the allowed size.", code="archive_bomb"
                )

            seen.add(rel)
            contents.files[rel] = raw
            contents.file_count += 1

    contents.total_uncompressed = running_total
    return contents


def build_zip(files: Mapping[str, bytes], *, compress: bool = True) -> bytes:
    """Create a deterministic ZIP from validated project-relative paths."""

    buffer = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", compression=mode) as archive:
        for path in sorted(files):
            rel = safe_project_path(path)
            info = zipfile.ZipInfo(rel)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = mode
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[path])
    return buffer.getvalue()


def detect_zip(data: bytes) -> bool:
    """Return ``True`` when ``data`` begins with the ZIP magic number."""

    return data[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}


def safe_archive_name(original: str, fallback: str = "project") -> str:
    """Derive a safe output name for a returned archive."""

    stem = (original or fallback).rsplit("/", 1)[-1]
    for suffix in (".zip",):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    stem = stem.strip() or fallback
    return f"{stem}_edited.zip"


def summarize_skipped(skipped: list[str], *, limit: int = 8) -> Optional[str]:
    if not skipped:
        return None
    shown = skipped[:limit]
    more = "" if len(skipped) <= limit else f" (+{len(skipped) - limit} more)"
    return ", ".join(shown) + more
