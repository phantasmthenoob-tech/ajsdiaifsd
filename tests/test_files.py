"""File handling: validation, archives, generation and editing."""

from __future__ import annotations

import io
import zipfile

import pytest

from config import FileLimits
from files.archive import build_zip, read_zip
from files.editor import EditError, apply_patches, output_filename, parse_patches
from files.generator import GenerationError, parse_file_blocks
from files.reader import decode_bytes, read_text_file
from files.validation import (
    FileValidationError,
    is_allowed_filename,
    safe_project_path,
    sanitize_filename,
    validate_generated_path,
    validate_upload,
)


def make_zip(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16  # symlink mode bits
            info.create_system = 3
            archive.writestr(info, "target.py")
    return buffer.getvalue()


# --------------------------------------------------------------------------- validation
def test_extension_allow_list():
    assert is_allowed_filename("main.py")
    assert is_allowed_filename("app.tsx")
    assert is_allowed_filename("Dockerfile")
    assert is_allowed_filename(".env.example")
    assert not is_allowed_filename("photo.png")
    assert not is_allowed_filename("archive.tar")


def test_sanitize_filename_strips_paths():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a/b/main.py") == "main.py"
    assert sanitize_filename("") == "file.txt"


def test_safe_project_path_rejects_traversal_and_absolute():
    assert safe_project_path("src/main.py") == "src/main.py"
    for bad in ("../evil.py", "/etc/passwd", "C:\\windows\\x.py", "a/../../b.py", "//server/share"):
        with pytest.raises(FileValidationError):
            safe_project_path(bad)


def test_validate_upload_rejects_binary_and_oversized():
    limits = FileLimits(max_attachment_bytes=100)
    with pytest.raises(FileValidationError):
        validate_upload(filename="evil.exe", size=10, limits=limits)
    with pytest.raises(FileValidationError):
        validate_upload(filename="big.py", size=10_000, limits=limits)
    with pytest.raises(FileValidationError):
        validate_upload(filename="mystery.xyz", size=10, limits=limits)
    assert validate_upload(filename="ok.py", size=10, limits=limits) == ".py"


def test_validate_generated_path_rejects_escape():
    assert validate_generated_path("pkg/mod.py") == "pkg/mod.py"
    with pytest.raises(FileValidationError):
        validate_generated_path("../../etc/cron.d/x")


# --------------------------------------------------------------------------- reading
def test_decode_bytes_handles_utf8_and_binary():
    text, _ = decode_bytes("héllo".encode("utf-8"))
    assert text == "héllo"
    with pytest.raises(FileValidationError):
        decode_bytes(b"\x00\x01\x02\xff\xfe")


def test_read_text_file_roundtrip():
    limits = FileLimits()
    read = read_text_file(filename="notes.txt", data=b"line1\nline2\n", limits=limits)
    assert read.line_count == 2
    assert read.extension == ".txt"


# --------------------------------------------------------------------------- archives
def test_read_zip_roundtrip():
    limits = FileLimits()
    data = build_zip({"src/main.py": b"print('x')\n", "README.md": b"# hi\n"})
    contents = read_zip(data, limits)
    assert set(contents.files) == {"src/main.py", "README.md"}
    assert contents.file_count == 2


def test_read_zip_rejects_traversal_and_absolute():
    limits = FileLimits()
    with pytest.raises(FileValidationError):
        read_zip(make_zip({"../evil.py": b"x"}), limits)
    with pytest.raises(FileValidationError):
        read_zip(make_zip({"/etc/passwd": b"x"}), limits)


def test_read_zip_rejects_symlinks():
    with pytest.raises(FileValidationError):
        read_zip(make_zip({"ok.py": b"x"}, symlink="link.py"), FileLimits())


def test_read_zip_rejects_bombs_and_excess_files():
    bomb_limits = FileLimits(max_archive_compression_ratio=5, max_archive_uncompressed_bytes=50_000_000)
    bomb = build_zip({"bomb.txt": b"\x00" * 5_000_000})
    with pytest.raises(FileValidationError):
        read_zip(bomb, bomb_limits)

    many_limits = FileLimits(max_archive_files=2)
    with pytest.raises(FileValidationError):
        read_zip(make_zip({"a.py": b"x", "b.py": b"y", "c.py": b"z"}), many_limits)


# --------------------------------------------------------------------------- generation
def test_parse_file_blocks():
    text = (
        "Here you go.\n\n"
        "### FILE: src/main.py\n```python\nprint('hi')\n```\n\n"
        "### FILE: README.md\n```markdown\n# Demo\n```\n"
    )
    project = parse_file_blocks(text, FileLimits())
    assert [f.path for f in project.files] == ["src/main.py", "README.md"]
    assert project.files[0].content == "print('hi')\n"


def test_parse_file_blocks_skips_traversal():
    text = "### FILE: ../evil.py\n```python\nx=1\n```\n"
    project = parse_file_blocks(text, FileLimits())
    assert project.is_empty()
    assert project.skipped


def test_parse_file_blocks_enforces_count_limit():
    text = "### FILE: a.py\n```\na\n```\n### FILE: b.py\n```\nb\n```\n"
    with pytest.raises(GenerationError):
        parse_file_blocks(text, FileLimits(max_generated_files=1))


def test_inline_filename_fence_format():
    text = "```python filename=src/app.py\nprint('x')\n```\n"
    project = parse_file_blocks(text, FileLimits())
    assert project.files[0].path == "src/app.py"


# --------------------------------------------------------------------------- editing
def test_output_filename_never_overwrites():
    assert output_filename("main.py") == "main_edited.py"
    assert output_filename("a/b/config.json") == "config_edited.json"
    assert output_filename("Makefile") == "Makefile_edited"


def test_patch_parsing_and_application():
    text = (
        "### PATCH: main.py\n"
        "<<<<<<< ORIGINAL\n    return a-b\n=======\n    return a+b\n>>>>>>> UPDATED\n"
    )
    sets = parse_patches(text)
    assert sets and sets[0].path == "main.py"
    result = apply_patches("def add(a,b):\n    return a-b\n", sets[0].patches)
    assert "return a+b" in result


def test_patch_errors_when_ambiguous_or_missing():
    with pytest.raises(EditError):
        apply_patches("x\nx\n", parse_patches("### PATCH: f\n<<<<<<< ORIGINAL\nx\n=======\ny\n>>>>>>> UPDATED\n")[0].patches)
    with pytest.raises(EditError):
        apply_patches("nothing here", parse_patches("### PATCH: f\n<<<<<<< ORIGINAL\nzzz\n=======\ny\n>>>>>>> UPDATED\n")[0].patches)
