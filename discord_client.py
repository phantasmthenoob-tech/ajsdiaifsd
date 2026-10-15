"""Discord integration: intents, message handling and slash commands.

This module is the only place that knows about discord.py.  All business logic
lives in :mod:`engine` and the context/file modules, which keeps the request
pipeline testable without a live Discord connection.

Owner authorization is enforced *inside* every context command handler by
comparing ``interaction.user.id`` against ``BOT_OWNER_ID`` - never a username,
nickname or role.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from context.models import Scope
from context.service import ContextError, ContextService
from discord_agent import AgentCog
from discord_infra import InfraCog
from engine import AttachmentPayload, ChatbotEngine, IncomingRequest, Response
from files.archive import detect_zip
from files.validation import FileValidationError, validate_upload
from utils import chunk_text, human_bytes

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
MAX_FILES_PER_MESSAGE = 10

SCOPE_CHOICES = [
    app_commands.Choice(name="all", value="all"),
    app_commands.Choice(name="user", value="user"),
    app_commands.Choice(name="channel", value="channel"),
    app_commands.Choice(name="server", value="server"),
]
ROLE_CHOICES = [
    app_commands.Choice(name="user", value="user"),
    app_commands.Choice(name="assistant", value="assistant"),
]
OPERATION_CHOICES = [
    app_commands.Choice(name="append", value="append"),
    app_commands.Choice(name="replace", value="replace"),
    app_commands.Choice(name="remove", value="remove"),
]


def build_intents() -> discord.Intents:
    """Return the minimal intents the bot needs.

    We enable only what is required: guild messages, direct messages and the
    privileged message-content intent (needed to read mention text).  We do NOT
    enable members or presences.
    """

    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.dm_messages = True
    intents.message_content = True
    return intents


def strip_bot_mentions(content: str, bot_id: int, *, strict: bool) -> str:
    """Remove this bot's mention(s) from a message body."""

    text = content
    for pattern in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        text = text.replace(pattern, " ")
    text = text.strip()
    if strict and content.strip().startswith(("<@", "<@!")):
        return text
    return text


class AIChatCog(commands.Cog):
    """Slash commands and message listeners."""

    def __init__(self, chatbot: "DiscordChatbot") -> None:
        self.chatbot = chatbot
        self.config: Config = chatbot.config
        self.engine: ChatbotEngine = chatbot.engine
        self.context_service: ContextService = chatbot.context_service

    # -- authorization ------------------------------------------------------
    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if self.config.is_owner(interaction.user.id):
            return True
        command_name = interaction.command.name if interaction.command else "unknown"
        # Log the attempt without logging any context contents.
        logger.warning(
            "Denied owner-only command '%s' for user id %s (not the configured owner).",
            command_name,
            interaction.user.id,
        )
        await interaction.response.send_message(
            "That command is restricted to the bot owner.", ephemeral=True
        )
        return False

    def _resolve_target(
        self,
        scope: str,
        *,
        target_user: Optional[discord.User],
        target_channel: Optional[discord.abc.GuildChannel],
        guild_id: Optional[int],
    ) -> Optional[int]:
        if scope == "all":
            return None
        if scope == "user":
            return target_user.id if target_user else None
        if scope == "channel":
            return target_channel.id if target_channel else None
        if scope == "server":
            return guild_id
        return None

    # -- AI commands --------------------------------------------------------
    @app_commands.command(name="ask", description="Ask the AI assistant anything.")
    @app_commands.describe(
        prompt="What you want to ask or request.",
        private="Only you can see the reply (default: false).",
    )
    async def ask(
        self,
        interaction: discord.Interaction,
        prompt: str,
        private: bool = False,
    ) -> None:
        await self._run_ai_command(interaction, prompt, ephemeral=private)

    @app_commands.command(name="code", description="Ask the AI to generate or edit code as files.")
    @app_commands.describe(prompt="Describe the code to generate or the change to make.")
    async def code(self, interaction: discord.Interaction, prompt: str) -> None:
        await self._run_ai_command(
            interaction,
            f"Create or edit code as files: {prompt}",
            ephemeral=False,
        )

    async def _run_ai_command(
        self, interaction: discord.Interaction, prompt: str, *, ephemeral: bool
    ) -> None:
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        attachments: list[AttachmentPayload] = []
        skipped: list[str] = []
        for attachment in interaction.message.attachments if interaction.message else []:
            payload = await self._download_attachment(attachment, skipped)
            if payload:
                attachments.append(payload)

        request = self._build_request(interaction, prompt, attachments)
        response = await self.engine.handle(request)
        await self._send_response(interaction, response, ephemeral=ephemeral, extra_notes=skipped)

    # -- context commands (owner only) --------------------------------------
    @app_commands.command(
        name="setcontext", description="Owner only: add, replace, or remove stored context."
    )
    @app_commands.choices(scope=SCOPE_CHOICES, role=ROLE_CHOICES, operation=OPERATION_CHOICES)
    @app_commands.describe(
        scope="Where the context applies.",
        content="The context text to store.",
        role="user for facts/notes, assistant for prior AI replies.",
        operation="append (default), replace, or remove.",
        label="Optional label so the entry can be updated by name.",
        target_user="Required for scope=user.",
        target_channel="Required for scope=channel.",
    )
    async def setcontext(
        self,
        interaction: discord.Interaction,
        scope: str,
        content: Optional[str] = None,
        role: str = "user",
        operation: str = "append",
        label: Optional[str] = None,
        target_user: Optional[discord.User] = None,
        target_channel: Optional[discord.abc.GuildChannel] = None,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        target = self._resolve_target(
            scope,
            target_user=target_user,
            target_channel=target_channel,
            guild_id=interaction.guild_id,
        )
        try:
            result = await self.context_service.set_context(
                scope=scope,
                content=content,
                operation=operation,
                target=target,
                role=role,
                label=label,
                actor_id=interaction.user.id,
            )
        except ContextError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {result.describe()}", ephemeral=True)

    @app_commands.command(name="listcontext", description="Owner only: list stored context.")
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.describe(scope="Optional scope filter.", target_user="For scope=user.", target_channel="For scope=channel.")
    async def listcontext(
        self,
        interaction: discord.Interaction,
        scope: Optional[str] = None,
        target_user: Optional[discord.User] = None,
        target_channel: Optional[discord.abc.GuildChannel] = None,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        try:
            target = (
                self._resolve_target(
                    scope,
                    target_user=target_user,
                    target_channel=target_channel,
                    guild_id=interaction.guild_id,
                )
                if scope
                else None
            )
            entries = await self.context_service.list_context(scope=scope, target=target, limit=100)
        except ContextError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        if not entries:
            await interaction.response.send_message("No context entries found.", ephemeral=True)
            return
        lines = []
        for entry in entries:
            where = entry.scope.value if entry.target_id is None else f"{entry.scope.value}:{entry.target_id}"
            label = f" [{entry.label}]" if entry.label else ""
            preview = entry.content if len(entry.content) <= 80 else entry.content[:77] + "..."
            lines.append(f"`#{entry.id}` **{where}**/{entry.role.value}{label}: {preview}")
        for chunk in chunk_text("\n".join(lines), DISCORD_MESSAGE_LIMIT - 20):
            await interaction.response.send_message(chunk, ephemeral=True) if not interaction.response.is_done() else await interaction.followup.send(chunk, ephemeral=True)

    @app_commands.command(name="getcontext", description="Owner only: show a context entry by ID or label.")
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.describe(id="Entry ID.", label="Entry label (requires scope).", scope="Scope for label lookup.", target_user="For scope=user.", target_channel="For scope=channel.")
    async def getcontext(
        self,
        interaction: discord.Interaction,
        id: Optional[int] = None,
        label: Optional[str] = None,
        scope: Optional[str] = None,
        target_user: Optional[discord.User] = None,
        target_channel: Optional[discord.abc.GuildChannel] = None,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        try:
            target = (
                self._resolve_target(
                    scope,
                    target_user=target_user,
                    target_channel=target_channel,
                    guild_id=interaction.guild_id,
                )
                if scope
                else None
            )
            entries = await self.context_service.get_context(
                entry_id=id, label=label, scope=scope, target=target
            )
        except ContextError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        if not entries:
            await interaction.response.send_message("No matching context entry.", ephemeral=True)
            return
        entry = entries[0]
        where = entry.scope.value if entry.target_id is None else f"{entry.scope.value}:{entry.target_id}"
        body = (
            f"**Entry #{entry.id}**\nscope: `{where}` | role: `{entry.role.value}` | "
            f"label: `{entry.label or '-'}`\n\n{entry.content}"
        )
        for chunk in chunk_text(body, DISCORD_MESSAGE_LIMIT - 10):
            if not interaction.response.is_done():
                await interaction.response.send_message(chunk, ephemeral=True)
            else:
                await interaction.followup.send(chunk, ephemeral=True)

    @app_commands.command(name="deletecontext", description="Owner only: delete a context entry by ID.")
    @app_commands.describe(id="The entry ID to delete.")
    async def deletecontext(self, interaction: discord.Interaction, id: int) -> None:
        if not await self._require_owner(interaction):
            return
        deleted = await self.context_service.delete_context(id)
        message = f"🗑️ Deleted entry #{id}." if deleted else f"No entry with ID {id}."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="clearcontext", description="Owner only: clear all context in a scope.")
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.describe(scope="Scope to clear.", confirm="Must be true to actually clear.", target_user="For scope=user.", target_channel="For scope=channel.")
    async def clearcontext(
        self,
        interaction: discord.Interaction,
        scope: str,
        confirm: bool = False,
        target_user: Optional[discord.User] = None,
        target_channel: Optional[discord.abc.GuildChannel] = None,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        if not confirm:
            await interaction.response.send_message(
                "This will delete stored context. Re-run with `confirm:true` to proceed.",
                ephemeral=True,
            )
            return
        try:
            target = self._resolve_target(
                scope,
                target_user=target_user,
                target_channel=target_channel,
                guild_id=interaction.guild_id,
            )
            removed = await self.context_service.clear_context(scope=scope, target=target)
        except ContextError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"🧹 Removed {removed} entries.", ephemeral=True)

    @app_commands.command(name="exportcontext", description="Owner only: export context as a JSON file.")
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.describe(scope="Optional scope to export.", target_user="For scope=user.", target_channel="For scope=channel.")
    async def exportcontext(
        self,
        interaction: discord.Interaction,
        scope: Optional[str] = None,
        target_user: Optional[discord.User] = None,
        target_channel: Optional[discord.abc.GuildChannel] = None,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        try:
            target = (
                self._resolve_target(
                    scope,
                    target_user=target_user,
                    target_channel=target_channel,
                    guild_id=interaction.guild_id,
                )
                if scope
                else None
            )
            payload = await self.context_service.export_context(scope=scope, target=target)
        except ContextError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        data = json.dumps(payload, indent=2).encode("utf-8")
        file = discord.File(io.BytesIO(data), filename="context_export.json")
        await interaction.response.send_message(
            f"Exported {payload['count']} entries.", file=file, ephemeral=True
        )

    @app_commands.command(name="importcontext", description="Owner only: import context from a JSON attachment.")
    @app_commands.describe(attachment="A JSON file previously exported with /exportcontext.", replace="Replace existing entries in the affected scopes.")
    async def importcontext(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment,
        replace: bool = False,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        if attachment.size > self.config.files.max_attachment_bytes:
            await interaction.response.send_message("❌ That file is too large.", ephemeral=True)
            return
        try:
            raw = await attachment.read()
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            await interaction.response.send_message(
                "❌ That file is not valid JSON.", ephemeral=True
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message("❌ Could not download that file.", ephemeral=True)
            return
        try:
            imported = await self.context_service.import_context(
                payload, actor_id=interaction.user.id, replace=replace
            )
        except ContextError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Imported {imported} context entries.", ephemeral=True
        )

    # -- message handling ---------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        bot_user = self.chatbot.bot.user
        if bot_user is None or message.author.id == bot_user.id:
            return

        summoned, prompt = self._summoned(message, bot_user.id)
        if not summoned:
            return

        skipped: list[str] = []
        attachments: list[AttachmentPayload] = []
        for attachment in message.attachments[:5]:
            payload = await self._download_attachment(attachment, skipped)
            if payload:
                attachments.append(payload)

        request = IncomingRequest(
            user_id=message.author.id,
            user_name=getattr(message.author, "display_name", message.author.name),
            prompt=prompt,
            guild_id=message.guild.id if message.guild else None,
            channel_id=message.channel.id,
            kind="channel" if message.guild else "dm",
            attachments=attachments,
            is_owner=self.config.is_owner(message.author.id),
        )

        try:
            async with message.channel.typing():
                response = await self.engine.handle(request)
            await self._deliver(message.channel, response, extra_notes=skipped)
        except discord.HTTPException:
            logger.warning("Discord rejected a message while replying in channel %s", message.channel.id)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Unhandled error while replying to a message")

    def _summoned(self, message: discord.Message, bot_id: int) -> tuple[bool, str]:
        """Decide whether the bot was summoned and return the cleaned prompt."""

        content = message.content or ""
        mentioned = any(user.id == bot_id for user in message.mentions)
        starts_with_mention = content.strip().startswith((f"<@{bot_id}>", f"<@!{bot_id}>"))

        if mentioned:
            if self.config.conversation.strict_mention and not starts_with_mention:
                # Mentioned mid-sentence; likely not addressed to us.
                if not self._is_reply_to_bot(message, bot_id):
                    return False, ""
            return True, strip_bot_mentions(content, bot_id, strict=starts_with_mention)

        if (
            self.config.conversation.enable_reply_conversations
            and self._is_reply_to_bot(message, bot_id)
            and content.strip()
        ):
            return True, content.strip()

        return False, ""

    @staticmethod
    def _is_reply_to_bot(message: discord.Message, bot_id: int) -> bool:
        reference = message.reference
        if reference is None:
            return False
        resolved = reference.resolved
        if isinstance(resolved, discord.Message):
            return resolved.author.id == bot_id
        return False

    # -- delivery helpers ---------------------------------------------------
    def _build_request(
        self,
        interaction: discord.Interaction,
        prompt: str,
        attachments: list[AttachmentPayload],
    ) -> IncomingRequest:
        return IncomingRequest(
            user_id=interaction.user.id,
            user_name=getattr(interaction.user, "display_name", interaction.user.name),
            prompt=prompt,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            kind="channel" if interaction.guild_id else "dm",
            attachments=attachments,
            is_owner=self.config.is_owner(interaction.user.id),
        )

    async def _download_attachment(
        self, attachment: discord.Attachment, skipped: list[str]
    ) -> Optional[AttachmentPayload]:
        limit = (
            self.config.files.max_archive_bytes
            if attachment.filename.lower().endswith(".zip")
            else self.config.files.max_attachment_bytes
        )
        if attachment.size > limit:
            skipped.append(f"`{attachment.filename}` was too large ({human_bytes(attachment.size)}).")
            return None
        try:
            data = await attachment.read()
        except discord.HTTPException:
            skipped.append(f"`{attachment.filename}` could not be downloaded.")
            return None
        try:
            validate_upload(
                filename=attachment.filename,
                size=len(data),
                limits=self.config.files,
                allow_archive=True,
            )
        except FileValidationError as exc:
            if detect_zip(data) or attachment.filename.lower().endswith(".zip"):
                # Size/extension already checked; let the archive reader decide.
                return AttachmentPayload(attachment.filename, data)
            skipped.append(exc.user_message)
            return None
        return AttachmentPayload(attachment.filename, data)

    async def _send_response(
        self,
        interaction: discord.Interaction,
        response: Response,
        *,
        ephemeral: bool,
        extra_notes: list[str],
    ) -> None:
        text = response.text
        if extra_notes:
            text += "\n\n" + "\n".join(f"⚠️ {note}" for note in extra_notes)
        chunks = chunk_text(text, DISCORD_MESSAGE_LIMIT)
        files = [
            discord.File(io.BytesIO(out.data), filename=out.filename)
            for out in response.files[:MAX_FILES_PER_MESSAGE]
        ]
        if not interaction.response.is_done():
            await interaction.response.send_message(chunks[0], files=files, ephemeral=ephemeral)
        else:
            await interaction.followup.send(chunks[0], files=files, ephemeral=ephemeral)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=ephemeral)

    async def _deliver(
        self, channel: discord.abc.Messageable, response: Response, *, extra_notes: list[str]
    ) -> None:
        text = response.text
        if extra_notes:
            text += "\n\n" + "\n".join(f"⚠️ {note}" for note in extra_notes)
        chunks = chunk_text(text, DISCORD_MESSAGE_LIMIT)
        files = [
            discord.File(io.BytesIO(out.data), filename=out.filename)
            for out in response.files[:MAX_FILES_PER_MESSAGE]
        ]
        await channel.send(chunks[0], files=files)
        for chunk in chunks[1:]:
            await channel.send(chunk)


class _ChatbotClient(commands.Bot):
    """Bot subclass that installs the cog and syncs slash commands on startup."""

    def __init__(self, chatbot: "DiscordChatbot", **kwargs) -> None:
        super().__init__(**kwargs)
        self.chatbot = chatbot

    async def setup_hook(self) -> None:
        # Called by discord.py after login and before READY, so the HTTP
        # session exists and command sync can succeed.
        await self.add_cog(AIChatCog(self.chatbot))
        if getattr(self.chatbot, "infra_service", None) is not None:
            await self.add_cog(InfraCog(self.chatbot))
        if getattr(self.chatbot, "agent_runtime", None) is not None:
            await self.add_cog(AgentCog(self.chatbot))

        @self.tree.error
        async def on_app_command_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            logger.warning("Slash command error: %s", error.__class__.__name__)
            message = "That command failed. Please try again."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except discord.HTTPException:  # pragma: no cover - best effort
                pass

        if self.chatbot.config.guild_ids:
            for guild_id in self.chatbot.config.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            logger.info("Synced slash commands to %d guild(s)", len(self.chatbot.config.guild_ids))
        else:
            await self.tree.sync()
            logger.info("Synced slash commands globally (may take up to an hour to appear)")

    async def on_ready(self) -> None:  # pragma: no cover - requires a live gateway
        logger.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?") )


class DiscordChatbot:
    """Owns the discord.py client and wires up the cog."""

    def __init__(
        self,
        *,
        config: Config,
        engine: ChatbotEngine,
        context_service: ContextService,
        infra_service: Optional[object] = None,
        agent_runtime: Optional[object] = None,
    ) -> None:
        self.config = config
        self.engine = engine
        self.context_service = context_service
        self.infra_service = infra_service
        self.agent_runtime = agent_runtime
        self.bot = _ChatbotClient(
            self,
            command_prefix=commands.when_mentioned,
            intents=build_intents(),
            help_command=None,
        )

    def run(self) -> None:
        """Start the bot (blocking). ``log_handler=None`` keeps our own logging config."""

        self.bot.run(self.config.discord_token, log_handler=None)
