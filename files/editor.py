"""Apply AI-generated edits to existing files.

Two edit styles are supported:

* **Full regeneration** - the model returns the entire edited file.  This is the
  fallback and is always safe to apply, though it rewrites more than necessary.
* **Minimal patches** - the model returns one or more search/replace blocks::

      ### PATCH: path/to/file.ext
      <<<<<<< ORIGINAL
      old lines
      =======
      new lines
      >>>>>>> UPDATED

  Each ``ORIGINAL`` block must match the file exactly once, otherwise the patch
  is rejected rather than guessing.  This keeps unrelated code untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

PATCH_HEADER_RE = re.compile(r"^[ \t]*#{2,4}[ \t]*PATCH:[ \t]*(.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
PATCH_BLOCK_RE = re.compile(
    r"<<<<<<< ?ORIGINAL[ \t]*\r?\n(.*?)\r?\n?=======[ \t]*\r?\n(.*?)\r?\n?>>>>>>> ?UPDATED",
    re.DOTALL,
)

EDIT_PROTOCOL = (
    "For small, targeted changes prefer returning minimal patches in this format:\n\n"
    "### PATCH: path/to/file.ext\n"
    "<<<<<<< ORIGINAL\n"
    "<exact existing lines>\n"
    "=======\n"
    "<replacement lines>\n"
    ">>>>>>> UPDATED\n\n"
    "Every ORIGINAL block must match the file exactly once. If a change is large or "
    "structural, return the complete edited file in a `### FILE:` block instead."
)


class EditError(Exception):
    """Raised when an edit cannot be applied safely."""

    def __init__(self, user_message: str, *, code: str = "edit_error") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


@dataclass
class Patch:
    original: str
    replacement: str


@dataclass
class PatchSet:
    path: str
    patches: list[Patch]


def output_filename(original: str, *, suffix: str = "_edited") -> str:
    """Derive an output name that never overwrites the uploaded file."""

    name = original.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name[1:]:
        stem, _, ext = name.rpartition(".")
        return f"{stem}{suffix}.{ext}"
    return f"{name}{suffix}"


def parse_patches(text: str) -> list[PatchSet]:
    """Parse every ``### PATCH:`` block from an AI response."""

    results: list[PatchSet] = []
    headers = list(PATCH_HEADER_RE.finditer(text))
    for index, header in enumerate(headers):
        start = header.end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        region = text[start:end]
        patches = [
            Patch(original=m.group(1), replacement=m.group(2))
            for m in PATCH_BLOCK_RE.finditer(region)
        ]
        if patches:
            results.append(PatchSet(path=header.group(1).strip().strip("`"), patches=patches))
    return results


def apply_search_replace(text: str, original: str, replacement: str, *, path: str = "file") -> str:
    """Replace exactly one occurrence of ``original`` with ``replacement``."""

    if not original:
        raise EditError(f"An empty ORIGINAL block was supplied for `{path}`.", code="empty_patch")
    occurrences = text.count(original)
    if occurrences == 0:
        raise EditError(
            f"The patch for `{path}` did not match the file; no changes were made.",
            code="patch_no_match",
        )
    if occurrences > 1:
        raise EditError(
            f"The patch for `{path}` matched {occurrences} places; refusing to guess.",
            code="patch_ambiguous",
        )
    return text.replace(original, replacement, 1)


def apply_patches(text: str, patches: list[Patch], *, path: str = "file") -> str:
    """Apply an ordered list of patches to ``text``.

    Patches are applied sequentially and each must apply cleanly; if any fails
    the whole edit is rejected (the original text is not modified in place).
    """

    result = text
    for patch in patches:
        result = apply_search_replace(result, patch.original, patch.replacement, path=path)
    return result


def has_patches(text: str) -> bool:
    return bool(parse_patches(text))
