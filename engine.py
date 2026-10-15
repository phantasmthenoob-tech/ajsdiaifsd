"""Framework-independent chatbot core.

:class:`ChatbotEngine` receives a normalised :class:`IncomingRequest` and returns
a :class:`Response`.  It knows nothing about discord.py, which keeps the whole
request pipeline unit-testable without a Discord connection.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

from ai.conversation import (
    Attachment,
    build_messages,
    estimate_tokens,
    summarize_messages,
)
from ai.provider import AIError, AIProvider
from config import Config
from context.models import Role
from context.repository import ConversationRepository, FileMetadataRepository
from context.service import ContextService
from files.archive import detect_zip, read_zip, safe_archive_name
from files.editor import (
    EDIT_PROTOCOL,
    EditError,
    apply_patches,
    output_filename,
    parse_patches,
)
from files.generator import (
    FENCE_RE,
    FILE_HEADER_RE,
    INLINE_NAME_RE,
    GenerationError,
    build_zip,
    parse_file_blocks,
)
from files.reader import decode_bytes
from files.validation import FileValidationError, basename, is_archive_filename
from sandbox.runner import SandboxRunner, StaticValidator
from utils import sha256_hex

logger = logging.getLogger(__name__)

EDIT_INTENT_WORDS = (
    "edit", "fix", "refactor", "update", "modify", "change", "add ", "convert",
    "patch", "rewrite", "rename", "debug", "remove", "insert", "improve", "optimi",
    "migrate", "upgrade", "reformat", "lint",
)

BOT_FOOTER = "\n\n_Static syntax validation was applied; no code was executed._"


class EngineError(Exception):
    """Raised for user-facing orchestration problems."""


# ---------------------------------------------------------------------------
# Request / response contracts
# ---------------------------------------------------------------------------
@dataclass
class AttachmentPayload:
    filename: str
    data: bytes


@dataclass
class IncomingRequest:
    user_id: int
    user_name: str
    prompt: str
    guild_id: Optional[int] = None
    channel_id: Optional[int] = None
    kind: str = "channel"  # 'channel' or 'dm'
    attachments: list[AttachmentPayload] = field(default_factory=list)
    is_owner: bool = False


@dataclass
class OutgoingFile:
    filename: str
    data: bytes
    description: str = ""


@dataclass
class Response:
    text: str
    files: list[OutgoingFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ephemeral: bool = False


@dataclass
class PreparedAttachments:
    for_prompt: list[Attachment]
    raw_text: dict[str, str]
    archive_name: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class ChatbotEngine:
    def __init__(
        self,
        *,
        config: Config,
        provider: AIProvider,
        context_service: ContextService,
        conversations: ConversationRepository,
        file_metadata: FileMetadataRepository,
        sandbox: Optional[SandboxRunner] = None,
        validator: Optional[StaticValidator] = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.context_service = context_service
        self.conversations = conversations
        self.file_metadata = file_metadata
        self.sandbox = sandbox
        self.validator = validator or StaticValidator()

    # -- public entry point -------------------------------------------------
    async def handle(self, request: IncomingRequest) -> Response:
        try:
            return await self._handle(request)
        except AIError as exc:
            logger.warning("AI provider error: %s", exc.__class__.__name__)
            return Response(
                text=(
                    "I couldn't reach the AI provider just now "
                    f"({exc.__class__.__name__}). Please try again in a moment."
                )
            )
        except (FileValidationError, GenerationError, EditError) as exc:
            return Response(text=f"I couldn't complete that: {exc.user_message}")
        except Exception:  # pragma: no cover - last-resort safety net
            logger.exception("Unexpected error while handling a request")
            return Response(
                text="Something went wrong while processing that request. "
                "The details are in the bot logs."
            )

    # -- pipeline -----------------------------------------------------------
    async def _handle(self, request: IncomingRequest) -> Response:
        conversation = await self.conversations.get_or_create(
            kind=request.kind,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            user_id=request.user_id,
        )

        prepared = await self._prepare_attachments(request)
        warnings = list(prepared.warnings)

        edit_mode = bool(prepared.for_prompt) and self._looks_like_edit(request.prompt)

        layers = await self.context_service.applicable_context(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            user_id=request.user_id,
        )
        history = await self.conversations.recent_messages(
            conversation.id, self.config.conversation.max_history_messages
        )

        system_extra = self._mode_instruction(edit_mode, bool(prepared.for_prompt))
        prompt_text = request.prompt.strip() or "(no message text)"
        if system_extra:
            prompt_text = f"{system_extra}\n\n{prompt_text}"

        build = build_messages(
            context_layers=layers,
            history=history,
            current_prompt=prompt_text,
            attachments=prepared.for_prompt,
            summary=conversation.summary,
            max_tokens=self.config.conversation.max_context_tokens,
            max_history_messages=self.config.conversation.max_history_messages,
            reserve_output_tokens=min(self.config.ai.max_tokens, 1500),
            include_author_names=(request.kind == "channel"),
        )
        if build.truncated:
            warnings.append(
                "Some older context or history was trimmed to fit the model's token budget."
            )

        result = await self.provider.complete(
            build.messages,
            temperature=self.config.ai.temperature,
            max_tokens=self.config.ai.max_tokens,
        )
        reply_text = result.text or ""

        files, file_warnings, explanation = await self._process_output(
            reply_text,
            edit_mode=edit_mode,
            request=request,
            prepared=prepared,
            conversation_id=conversation.id,
        )
        warnings.extend(file_warnings)

        chat_text = explanation or self._strip_file_blocks(reply_text).strip()
        if files:
            summary = ", ".join(f"`{f.filename}`" for f in files[:8])
            more = "" if len(files) <= 8 else f" (+{len(files) - 8} more)"
            chat_text = (
                f"{chat_text}\n\n**Attached:** {summary}{more}".strip()
            )
        if not chat_text and not files:
            chat_text = "I'm not sure how to help with that - could you add a little more detail?"
        if warnings:
            chat_text += "\n\n" + "\n".join(f"⚠️ {w}" for w in warnings)

        # Persist history (compact: storage note instead of raw file dumps).
        stored_user = prompt_text
        if prepared.for_prompt:
            stored_user += "\n[attachments: " + ", ".join(a.filename for a in prepared.for_prompt) + "]"
        await self.conversations.add_message(
            conversation.id,
            role=Role.USER,
            content=stored_user,
            author_id=request.user_id,
            author_name=request.user_name,
            token_estimate=estimate_tokens(stored_user),
        )
        stored_assistant = chat_text
        if files:
            stored_assistant += "\n[files: " + ", ".join(f.filename for f in files) + "]"
        await self.conversations.add_message(
            conversation.id,
            role=Role.ASSISTANT,
            content=stored_assistant,
            token_estimate=estimate_tokens(stored_assistant),
        )

        await self._maybe_summarize(conversation.id)

        return Response(text=chat_text[:6000] or "Done.", files=files, warnings=warnings)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _looks_like_edit(prompt: str) -> bool:
        lowered = f" {prompt.lower()} "
        return any(word in lowered for word in EDIT_INTENT_WORDS)

    @staticmethod
    def _mode_instruction(edit_mode: bool, has_files: bool) -> str:
        if edit_mode:
            return (
                "The user uploaded file(s) and wants them changed. Apply minimal, targeted "
                "edits and preserve unrelated code." + "\n\n" + EDIT_PROTOCOL
            )
        if has_files:
            return (
                "The user uploaded file(s) for review. Explain, analyse, or answer questions "
                "about them. Do not emit `### FILE:` blocks unless the user asked for changes."
            )
        return ""

    async def _prepare_attachments(self, request: IncomingRequest) -> PreparedAttachments:
        prepared = PreparedAttachments(for_prompt=[], raw_text={})
        if not request.attachments:
            return prepared

        limits = self.config.files
        if len(request.attachments) > 5:
            prepared.warnings.append("Only the first 5 attachments were processed.")
        attachments = request.attachments[:5]

        for payload in attachments:
            name = basename(payload.filename)
            try:
                if is_archive_filename(name) or detect_zip(payload.data):
                    archive = read_zip(payload.data, limits)
                    prepared.archive_name = name or "project.zip"
                    decoded = 0
                    for path, raw in archive.files.items():
                        try:
                            text, _ = decode_bytes(raw)
                        except FileValidationError:
                            continue
                        prepared.raw_text[path] = text
                        prepared.for_prompt.append(
                            Attachment(filename=path, content=text, note="from uploaded archive")
                        )
                        decoded += 1
                    await self.file_metadata.add_file(
                        direction="upload",
                        filename=prepared.archive_name,
                        mime_type="application/zip",
                        size_bytes=len(payload.data),
                        sha256=sha256_hex(payload.data),
                    )
                    if archive.skipped:
                        prepared.warnings.append(
                            f"Skipped {len(archive.skipped)} unsupported or oversized archive entries."
                        )
                    if decoded == 0:
                        prepared.warnings.append(
                            f"`{name}` contained no readable text files."
                        )
                else:
                    text, _ = decode_bytes(payload.data)
                    prepared.raw_text[name] = text
                    prepared.for_prompt.append(Attachment(filename=name, content=text))
                    await self.file_metadata.add_file(
                        direction="upload",
                        filename=name,
                        size_bytes=len(payload.data),
                        sha256=sha256_hex(payload.data),
                    )
            except FileValidationError as exc:
                prepared.warnings.append(exc.user_message)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to read attachment %s", name)
                prepared.warnings.append(f"Could not read `{name}`.")

        # Keep prompts focused: cap how much file text we forward at once.
        prepared.for_prompt = self._budget_attachments(prepared.for_prompt)
        return prepared

    @staticmethod
    def _budget_attachments(attachments: list[Attachment], *, max_chars: int = 120_000) -> list[Attachment]:
        budgeted: list[Attachment] = []
        used = 0
        for attachment in attachments:
            if used >= max_chars:
                break
            remaining = max_chars - used
            content = attachment.content
            if len(content) > remaining:
                content = content[:remaining] + "\n…[file truncated to fit the prompt budget]"
            budgeted.append(Attachment(attachment.filename, content, attachment.note))
            used += len(content)
        return budgeted

    async def _process_output(
        self,
        reply_text: str,
        *,
        edit_mode: bool,
        request: IncomingRequest,
        prepared: PreparedAttachments,
        conversation_id: int,
    ) -> tuple[list[OutgoingFile], list[str], str]:
        warnings: list[str] = []
        limits = self.config.files
        outputs: list[OutgoingFile] = []
        explanation = ""

        # 1) Minimal patches applied to uploaded files.
        patch_sets = parse_patches(reply_text) if prepared.raw_text else []
        if patch_sets:
            for patch_set in patch_sets:
                target = self._match_original(patch_set.path, prepared.raw_text)
                if target is None:
                    warnings.append(f"Ignored a patch for `{patch_set.path}` (no matching upload).")
                    continue
                original = prepared.raw_text[target]
                try:
                    edited = apply_patches(original, patch_set.patches, path=target)
                except EditError as exc:
                    warnings.append(exc.user_message)
                    continue
                out_name = output_filename(target)
                outputs.append(OutgoingFile(out_name, edited.encode("utf-8"), f"edited from {target}"))

        # 2) Full files returned as `### FILE:` blocks.
        project = parse_file_blocks(reply_text, limits)
        if not project.is_empty():
            file_map = {f.path: f.content for f in project.files}
            report = self.validator.validate(file_map)
            if not report.ok:
                for failure in report.failures():
                    warnings.append(f"Syntax check failed for `{failure.path}`: {failure.detail}")
            outputs.extend(
                OutgoingFile(f.path, f.content.encode("utf-8"), "generated") for f in project.files
            )
        elif project.skipped:
            warnings.append(
                "Some generated files were skipped: " + ", ".join(project.skipped[:6])
            )

        # 3) Record + package.
        if not outputs:
            return [], warnings, self._strip_file_blocks(reply_text).strip()

        # De-duplicate by filename, keeping the last occurrence.
        deduped: dict[str, OutgoingFile] = {}
        for out in outputs:
            deduped[out.filename] = out
        outputs = list(deduped.values())

        for out in outputs:
            await self.file_metadata.add_file(
                direction="edited" if edit_mode else "generated",
                filename=out.filename,
                conversation_id=conversation_id,
                size_bytes=len(out.data),
                sha256=sha256_hex(out.data),
            )

        if len(outputs) > 1:
            # Multiple files -> a single ZIP that preserves structure.
            try:
                archive_bytes = build_zip({f.filename: f.data for f in outputs})
                zip_name = (
                    safe_archive_name(prepared.archive_name)
                    if prepared.archive_name
                    else "generated_project.zip"
                )
                return (
                    [OutgoingFile(zip_name, archive_bytes, f"{len(outputs)} files")],
                    warnings,
                    self._strip_file_blocks(reply_text).strip(),
                )
            except (GenerationError, FileValidationError):
                warnings.append("Could not package the files into a ZIP; returning them individually.")

        # Enforce per-file size when returning individually.
        filtered: list[OutgoingFile] = []
        for out in outputs:
            if len(out.data) > limits.max_generated_file_bytes:
                warnings.append(f"`{out.filename}` exceeded the size limit and was not attached.")
                continue
            filtered.append(out)

        if not filtered:
            warnings.append("No files could be attached.")
        return filtered, warnings, self._strip_file_blocks(reply_text).strip()

    @staticmethod
    def _match_original(path: str, raw_text: Mapping[str, str]) -> Optional[str]:
        path_norm = path.strip().lstrip("./")
        if path_norm in raw_text:
            return path_norm
        base = basename(path_norm)
        candidates = [key for key in raw_text if basename(key) == base]
        if len(candidates) == 1:
            return candidates[0]
        for key in raw_text:
            if key.endswith(path_norm):
                return key
        return None

    @staticmethod
    def _strip_file_blocks(text: str) -> str:
        ranges: list[tuple[int, int]] = []
        for match in FILE_HEADER_RE.finditer(text):
            fence = FENCE_RE.search(text, match.end())
            end = fence.end() if fence else match.end()
            ranges.append((match.start(), end))
        if not ranges:
            text = INLINE_NAME_RE.sub("", text)
            return re.sub(r"\n{3,}", "\n\n", text)

        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        out: list[str] = []
        cursor = 0
        for start, end in merged:
            out.append(text[cursor:start])
            cursor = end
        out.append(text[cursor:])
        cleaned = "".join(out)
        cleaned = INLINE_NAME_RE.sub("", cleaned)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    async def _maybe_summarize(self, conversation_id: int) -> None:
        max_history = self.config.conversation.max_history_messages
        count = await self.conversations.count_messages(conversation_id)
        if count <= self.config.conversation.summarize_after_messages:
            return
        conversation = await self.conversations.get(conversation_id)
        if conversation is None:
            return
        older = await self.conversations.messages_since(
            conversation_id, conversation.summary_upto, limit=200
        )
        if len(older) <= max_history:
            return
        to_summarize = older[: len(older) - max_history]
        summary = await summarize_messages(
            self.provider,
            to_summarize,
            model=None,
            existing_summary=conversation.summary,
        )
        if summary:
            await self.conversations.set_summary(conversation_id, summary, to_summarize[-1].id or 0)
