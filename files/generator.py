"""Turn AI responses into real files and project archives.

The model is asked (see :mod:`ai.conversation`) to emit files as::

    ### FILE: relative/path.ext
    ```lang
    <contents>
    ```

This module parses those blocks, validates every path and size against the
configured limits, and packages the result into a ZIP that preserves structure.
"""

from __future__ import annotations

import io
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from config import FileLimits
from files.validation import FileValidationError, basename, validate_generated_path
from utils import human_bytes

FILE_HEADER_RE = re.compile(r"^[ \t]*#{2,4}[ \t]*FILE:[ \t]*(.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
INLINE_NAME_RE = re.compile(
    r"```[^\n`]*\bfilename=([^\s`]+)[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE
)
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


class GenerationError(Exception):
    """Raised when a generated project cannot be built safely."""

    def __init__(self, user_message: str, *, code: str = "generation_error") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


@dataclass
class GeneratedFile:
    path: str
    content: str

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))


@dataclass
class GeneratedProject:
    files: list[GeneratedFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.files

    def as_mapping(self) -> dict[str, bytes]:
        return {f.path: f.content.encode("utf-8") for f in self.files}


def _normalise_content(text: str) -> str:
    if not text.endswith("\n"):
        text += "\n"
    return text


def _clean_path(raw: str) -> Optional[str]:
    raw = raw.strip().strip("`").strip().strip("\"'")
    if not raw:
        return None
    try:
        return validate_generated_path(raw)
    except FileValidationError:
        return None


def parse_file_blocks(text: str, limits: FileLimits) -> GeneratedProject:
    """Extract every ``### FILE:`` block (and ``filename=`` fences) from a response."""

    project = GeneratedProject()
    seen: dict[str, GeneratedFile] = {}
    skipped: list[str] = []
    order: list[str] = []

    # Primary format: a FILE header followed by the next fenced code block.
    for match in FILE_HEADER_RE.finditer(text):
        raw_path = match.group(1)
        path = _clean_path(raw_path)
        if not path:
            skipped.append(basename(raw_path) or raw_path.strip())
            continue
        fence = FENCE_RE.search(text, match.end())
        if not fence:
            tail = text[match.end():]
            next_header = FILE_HEADER_RE.search(tail)
            body = tail[: next_header.start()] if next_header else tail
            content = body.strip("\n")
            if not content:
                skipped.append(path)
                continue
        else:
            # Only accept the fence if it starts before the next header.
            next_header = FILE_HEADER_RE.search(text, match.end())
            if next_header and next_header.start() < fence.start():
                continue
            content = fence.group(1)
        if path not in seen:
            order.append(path)
        seen[path] = GeneratedFile(path=path, content=_normalise_content(content))

    # Secondary format: ```lang filename=path
    for match in INLINE_NAME_RE.finditer(text):
        path = _clean_path(match.group(1))
        if not path:
            skipped.append(match.group(1).strip())
            continue
        if path not in seen:
            order.append(path)
            seen[path] = GeneratedFile(path=path, content=_normalise_content(match.group(2)))

    if len(seen) > limits.max_generated_files:
        raise GenerationError(
            f"The model produced {len(seen)} files; the limit is {limits.max_generated_files}. "
            "Ask for a smaller, more focused change.",
            code="too_many_files",
        )

    for path in order:
        file = seen[path]
        if file.size_bytes > limits.max_generated_file_bytes:
            skipped.append(f"{path} (too large)")
            continue
        project.files.append(file)

    project.skipped = skipped
    return project


def write_to_directory(files: Mapping[str, str] | list[GeneratedFile], base_dir: Path) -> list[Path]:
    """Write generated files under ``base_dir``, creating directories as needed.

    Paths are re-validated here so a traversal attempt that slipped through
    parsing still cannot escape ``base_dir``.
    """

    base = Path(base_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    items: Mapping[str, str]
    if isinstance(files, list):
        items = {f.path: f.content for f in files}
    else:
        items = files

    for raw_path, content in items.items():
        rel = validate_generated_path(raw_path)
        target = (base / rel).resolve()
        if base != target and base not in target.parents:
            raise GenerationError("Refusing to write outside the workspace.", code="traversal")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written


def ensure_readme(files: list[GeneratedFile], *, title: str = "Generated project") -> list[GeneratedFile]:
    """Add a minimal README when a multi-file project lacks one."""

    if len(files) <= 1:
        return files
    if any(basename(f.path).lower().startswith("readme") for f in files):
        return files
    listing = "\n".join(f"- `{f.path}`" for f in files)
    readme = GeneratedFile(
        path="README.md",
        content=_normalise_content(
            f"# {title}\n\n"
            "This project was generated by a Discord AI chatbot.\n\n"
            "## Files\n\n"
            f"{listing}\n\n"
            "> Generated code was syntax-checked where possible but not executed "
            "unless you were told otherwise.\n"
        ),
    )
    return [readme, *files]


def build_zip(files: Mapping[str, str] | list[GeneratedFile], *, source_name: str = "project") -> bytes:
    """Package generated files into a ZIP that preserves directory structure."""

    items: dict[str, bytes]
    if isinstance(files, list):
        items = {f.path: f.content.encode("utf-8") for f in files}
    else:
        items = {path: (content.encode("utf-8") if isinstance(content, str) else content) for path, content in files.items()}

    if not items:
        raise GenerationError("There was nothing to package into an archive.", code="empty_project")

    buffer = io.BytesIO()
    stem = basename(source_name) or "project"
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(items):
            rel = validate_generated_path(path)
            info = zipfile.ZipInfo(rel)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.external_attr = 0o644 << 16
            archive.writestr(info, items[path])
    return buffer.getvalue()


def make_temp_workspace(prefix: str = "discord-bot-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def summarize_project(project: GeneratedProject) -> str:
    if project.is_empty():
        return "No files were produced."
    total = sum(f.size_bytes for f in project.files)
    return f"{len(project.files)} file(s), {human_bytes(total)} total"
