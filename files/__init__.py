"""Safe file reading, editing, generation and packaging."""

from files.archive import ArchiveContents, build_zip as build_archive_zip, read_zip, safe_archive_name
from files.editor import EditError, Patch, PatchSet, apply_patches, output_filename, parse_patches
from files.generator import (
    GeneratedFile,
    GeneratedProject,
    GenerationError,
    build_zip as build_project_zip,
    ensure_readme,
    make_temp_workspace,
    parse_file_blocks,
    summarize_project,
    write_to_directory,
)
from files.reader import ReadFile, decode_bytes, read_text_file
from files.validation import (
    FileValidationError,
    is_allowed_filename,
    is_archive_filename,
    safe_project_path,
    sanitize_filename,
    validate_generated_path,
    validate_upload,
)

__all__ = [
    "ArchiveContents",
    "read_zip",
    "build_archive_zip",
    "safe_archive_name",
    "EditError",
    "Patch",
    "PatchSet",
    "apply_patches",
    "output_filename",
    "parse_patches",
    "GeneratedFile",
    "GeneratedProject",
    "GenerationError",
    "build_project_zip",
    "ensure_readme",
    "make_temp_workspace",
    "parse_file_blocks",
    "summarize_project",
    "write_to_directory",
    "ReadFile",
    "decode_bytes",
    "read_text_file",
    "FileValidationError",
    "is_allowed_filename",
    "is_archive_filename",
    "safe_project_path",
    "sanitize_filename",
    "validate_generated_path",
    "validate_upload",
]
