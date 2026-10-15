"""Validation helpers for filenames, extensions, sizes and paths.

Every rejection raises :class:`FileValidationError`, whose ``user_message`` is
safe to show to a Discord user (it never contains internal details).
"""

from __future__ import annotations

import os
import posixpath
import re
from typing import Iterable, Optional

from config import FileLimits


class FileValidationError(Exception):
    """A file was rejected. ``user_message`` is safe for end users."""

    def __init__(self, user_message: str, *, code: str = "invalid_file") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


# ---------------------------------------------------------------------------
# Allow-lists
# ---------------------------------------------------------------------------
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Python
        ".py", ".pyw", ".pyi", ".pyx",
        # JS / TS
        ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte", ".astro",
        # JVM / systems / mobile
        ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
        ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx", ".cs", ".rs", ".go",
        ".swift", ".m", ".mm", ".dart", ".ex", ".exs", ".erl", ".hs", ".clj",
        ".lua", ".pl", ".pm", ".r", ".jl", ".nim", ".zig", ".v", ".sv", ".vhd",
        ".f90", ".f95", ".f", ".for", ".cbl", ".ada", ".adb", ".ads", ".pas", ".pp",
        ".d", ".cshtml", ".vb", ".asm", ".s",
        # Web & data
        ".html", ".htm", ".css", ".scss", ".sass", ".less",
        ".json", ".jsonc", ".json5", ".ndjson",
        ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg", ".conf", ".config",
        ".properties", ".props", ".targets", ".resx", ".plist",
        ".csv", ".tsv", ".txt", ".text", ".log", ".md", ".markdown", ".rst", ".adoc",
        ".sql", ".graphql", ".gql", ".proto", ".avsc", ".tf", ".tfvars", ".hcl",
        # Shells & scripts
        ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd",
        # Build / project files
        ".gradle", ".csproj", ".vbproj", ".fsproj", ".sln", ".pro", ".vcxproj",
        ".cmake", ".mk", ".make", ".makefile", ".dockerfile", ".lock", ".sum", ".mod",
        ".iml", ".nuspec", ".nuspec",
    }
)

ALLOWED_FILENAMES: frozenset[str] = frozenset(
    {
        ".env.example", ".env.sample", ".env.template",
        "dockerfile", "makefile", "gnumakefile", "cmakelists.txt",
        "procfile", "gemfile", "gemfile.lock", "rakefile", "justfile", "taskfile",
        ".gitignore", ".gitattributes", ".gitmodules", ".dockerignore",
        ".editorconfig", ".npmrc", ".nvmrc", ".node-version",
        ".prettierrc", ".eslintrc", ".eslintignore", ".babelrc", ".stylelintrc",
        ".python-version", ".ruby-version", ".tool-versions", ".terraformrc",
        ".bashrc", ".zshrc", ".profile",
        "license", "licence", "notice", "readme", "changelog", "contributing",
        "readme.md", "license.md", "license.txt", "authors", "codeowners",
    }
)

#: Extensions treated as archives (only ``.zip`` is handled).
ARCHIVE_EXTENSIONS: frozenset[str] = frozenset({".zip"})

#: Extensions we refuse outright because they are binary or dangerous.
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
        ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".class", ".jar",
        ".war", ".pyc", ".pyo", ".pyd", ".whl", ".egg", ".com", ".msi", ".dmg",
        ".app", ".apk", ".ipa",
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst",
        ".mp3", ".mp4", ".wav", ".ogg", ".flac", ".avi", ".mov", ".mkv", ".webm",
        ".m4a", ".aac", ".wmv",
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
        ".db", ".sqlite", ".sqlite3", ".mdb",
        ".psd", ".ai", ".sketch", ".blend", ".fbx", ".glb", ".gltf",
    }
)

# Windows drive letters, UNC paths and absolute paths are rejected everywhere.
_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f<>:\"|?*\\]")


def basename(filename: str) -> str:
    """Return the final path component, normalising separators."""

    return posixpath.basename(str(filename).replace("\\", "/").strip())


def extension_of(filename: str) -> str:
    """Return the lowercase extension including the dot, or ``''``."""

    name = basename(filename).lower()
    if name in ALLOWED_FILENAMES:
        return ""
    if name.startswith(".") and name.count(".") >= 1 and name in ALLOWED_FILENAMES:
        return ""
    _, ext = posixpath.splitext(name)
    return ext


def is_allowed_filename(filename: str) -> bool:
    name = basename(filename).lower()
    if not name or name in {".", ".."}:
        return False
    if name in ALLOWED_FILENAMES:
        return True
    ext = extension_of(name)
    return ext in ALLOWED_EXTENSIONS


def is_archive_filename(filename: str) -> bool:
    return extension_of(filename) in ARCHIVE_EXTENSIONS


def sanitize_filename(filename: str, *, fallback: str = "file.txt") -> str:
    """Reduce a name to a safe, single-component filename (no directories)."""

    name = basename(filename)
    name = _UNSAFE_NAME_RE.sub("_", name).strip().strip(".")
    if not name or name in {".", ".."}:
        return fallback
    # Collapse repeated dots while keeping a single leading dot for dotfiles.
    leading_dot = name.startswith(".")
    name = re.sub(r"\.{2,}", ".", name)
    if leading_dot and not name.startswith("."):
        name = "." + name
    return name[:200] or fallback


def safe_project_path(path: str) -> str:
    """Normalise a project-relative path, rejecting traversal and absolutes.

    Returns a POSIX-style relative path.  Raises :class:`FileValidationError`
    for anything that could escape a project directory.
    """

    raw = str(path).replace("\\", "/").strip()
    if not raw:
        raise FileValidationError("A file path was empty.", code="empty_path")
    if raw.startswith("/") or _DRIVE_RE.match(raw) or raw.startswith("//"):
        raise FileValidationError(
            f"Absolute paths are not allowed: `{basename(raw)}`", code="absolute_path"
        )
    normalised = posixpath.normpath(raw)
    if normalised in {".", ".."} or normalised.startswith("../") or "/../" in f"/{normalised}/":
        raise FileValidationError("Paths may not escape the project directory.", code="traversal")
    if normalised.startswith("/"):
        raise FileValidationError("Paths may not be absolute.", code="absolute_path")
    parts = [p for p in normalised.split("/") if p and p != "."]
    if any(part in {".."} for part in parts):
        raise FileValidationError("Paths may not contain `..`.", code="traversal")
    if len(parts) > 20:
        raise FileValidationError("Directory nesting is too deep.", code="too_deep")
    return "/".join(parts)


def is_probably_binary(data: bytes) -> bool:
    """Heuristically decide whether bytes represent binary content."""

    if not data:
        return False
    if b"\x00" in data[:8192]:
        return True
    sample = data[:8192]
    non_text = sum(
        1
        for byte in sample
        if byte < 9 or (13 < byte < 32) or (0x7F <= byte <= 0x9F)
    )
    return (non_text / len(sample)) > 0.30


def validate_size(size: int, limit: int, *, what: str = "file") -> None:
    if size < 0:
        raise FileValidationError("The file size could not be determined.", code="bad_size")
    if size > limit:
        raise FileValidationError(
            f"That {what} is too large ({size:,} bytes); the limit is {limit:,} bytes.",
            code="too_large",
        )


def validate_upload(
    *,
    filename: str,
    size: int,
    limits: FileLimits,
    allow_archive: bool = True,
) -> str:
    """Validate an uploaded attachment and return its lowercase extension."""

    name = basename(filename)
    if not name:
        raise FileValidationError("The attachment had no filename.", code="missing_name")

    size_limit = limits.max_archive_bytes if is_archive_filename(name) else limits.max_attachment_bytes
    if size_limit == limits.max_archive_bytes and not allow_archive:
        raise FileValidationError("Archive uploads are not accepted here.", code="archive_disabled")
    validate_size(size, size_limit, what="attachment")

    if extension_of(name) in BINARY_EXTENSIONS and not is_archive_filename(name):
        raise FileValidationError(
            f"`{name}` is a binary file type that cannot be read or edited as text.",
            code="binary_type",
        )
    if not is_allowed_filename(name):
        raise FileValidationError(
            f"`{name}` is not a supported text or source file type.",
            code="unsupported_type",
        )
    return extension_of(name)


def validate_generated_path(path: str) -> str:
    """Validate a path the AI asked us to create."""

    rel = safe_project_path(path)
    name = basename(rel)
    ext = extension_of(name)
    if ext in BINARY_EXTENSIONS and ext not in ARCHIVE_EXTENSIONS:
        raise FileValidationError(
            f"Generated file `{name}` has a binary extension and was rejected.",
            code="binary_type",
        )
    return rel
