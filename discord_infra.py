"""Discord surface for the infrastructure control plane.

Every command here is owner-only (numeric ``BOT_OWNER_ID``), ephemeral, and
delegates all real work to :class:`infra.service.InfraService`. Destructive
operations require explicit confirmation.
"""

from __future__ import annotations

import logging
import shlex
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from files.archive import detect_zip, read_zip
from files.reader import decode_bytes
from files.validation import FileValidationError
from infra.models import DeployPath, InfraError
from infra.service import InfraService
from permissions.models import PermissionScope, ResourceType

logger = logging.getLogger(__name__)

PROVIDER_CHOICES = [
    app_commands.Choice(name="render", value="render"),
    app_commands.Choice(name="fly", value="fly"),
]
PATH_CHOICES = [
    app_commands.Choice(name="git (commit + auto-deploy)", value="git"),
    app_commands.Choice(name="docker (build + push image)", value="docker"),
]


def scope_key_for(guild_id: Optional[int]) -> str:
    """Targets are isolated per Discord server (or a single DM scope)."""

    return f"g{guild_id}" if guild_id else "owner"


def expand_payloads(payloads: list[tuple[str, bytes]], limits) -> dict[str, str]:
    """Flatten attachments and ZIPs into a ``{path: text}`` mapping."""

    out: dict[str, str] = {}
    for filename, data in payloads:
        if filename.lower().endswith(".zip") or detect_zip(data):
            if len(data) > limits.max_archive_bytes:
                raise InfraError(f"`{filename}` is larger than the archive limit.")
            contents = read_zip(data, limits)
            for path, raw in contents.files.items():
                try:
                    out[path], _ = decode_bytes(raw)
                except FileValidationError:
                    continue
            continue
        try:
            out[filename], _ = decode_bytes(data)
        except FileValidationError as exc:
            raise InfraError(exc.user_message) from exc
    return out


class InfraCog(commands.Cog):
    """`/vm` subgroup - owner-only infrastructure operations."""

    group = app_commands.Group(
        name="vm", description="Owner-only: manage and deploy to compute targets."
    )

    def __init__(self, chatbot) -> None:
        self.chatbot = chatbot
        self.config: Config = chatbot.config
        self.infra: Optional[InfraService] = chatbot.infra_service
        self.engine = chatbot.engine

    # -- authorization ------------------------------------------------------
    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if self.config.is_owner(interaction.user.id):
            return True
        command_name = interaction.command.qualified_name if interaction.command else "vm"
        logger.warning(
            "Denied owner-only command '%s' for user id %s (not the configured owner).",
            command_name,
            interaction.user.id,
        )
        await interaction.response.send_message(
            "That command is restricted to the bot owner.", ephemeral=True
        )
        return False

    def _require_infra(self) -> InfraService:
        if self.infra is None:
            raise InfraError("The infrastructure control plane is not configured.")
        return self.infra

    # -- commands -----------------------------------------------------------
    @group.command(name="create", description="Create a new compute target.")
    @app_commands.describe(
        name="Target name (lowercase letters, digits, hyphens).",
        provider="Which provider to provision on.",
    )
    @app_commands.choices(provider=PROVIDER_CHOICES)
    async def create(
        self, interaction: discord.Interaction, name: str, provider: str = "render"
    ) -> None:
        if not await self._require_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            target = await self._require_infra().create(
                scope_key=scope_key_for(interaction.guild_id), provider=provider, name=name
            )
        except InfraError as exc:
            await interaction.followup.send(f"❌ {exc.user_message}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Created {target.short}\nID: `{target.external_id}`"
            + (f"\nURL: {target.url}" if target.url else ""),
            ephemeral=True,
        )

    @group.command(name="list", description="List targets and show the active one.")
    async def list(self, interaction: discord.Interaction) -> None:
        if not await self._require_owner(interaction):
            return
        infra = self._require_infra()
        scope = scope_key_for(interaction.guild_id)
        targets = await infra.list(scope_key=scope)
        active = await infra.active(scope_key=scope)
        if not targets:
            await interaction.response.send_message(
                "No targets yet. Use `/vm create` first.", ephemeral=True
            )
            return
        lines = []
        for target in targets:
            marker = "▶️" if active and target.name == active.name else "•"
            url = f" - {target.url}" if target.url else ""
            lines.append(f"{marker} `{target.name}` ({target.provider}, {target.status}){url}")
        body = "\n".join(lines) + f"\n\n_{infra.status_line()}_"
        await interaction.response.send_message(body, ephemeral=True)

    @group.command(name="status", description="Show a target's live status.")
    @app_commands.describe(name="Target name (defaults to the active target).")
    async def status(self, interaction: discord.Interaction, name: Optional[str] = None) -> None:
        if not await self._require_owner(interaction):
            return
        infra = self._require_infra()
        scope = scope_key_for(interaction.guild_id)
        target = (
            await infra.registry.get(scope, name) if name else await infra.active(scope_key=scope)
        )
        if target is None:
            await interaction.response.send_message("No such target.", ephemeral=True)
            return
        provider = infra.providers.get(target.provider)
        live = await provider.get(target.external_id) if provider else None
        status = live.status if live else target.status
        await interaction.response.send_message(
            f"`{target.name}` ({target.provider}) → **{status}**", ephemeral=True
        )

    @group.command(name="inspect", description="Show full details for a target.")
    @app_commands.describe(name="Target name (defaults to the active target).")
    async def inspect(self, interaction: discord.Interaction, name: Optional[str] = None) -> None:
        if not await self._require_owner(interaction):
            return
        infra = self._require_infra()
        scope = scope_key_for(interaction.guild_id)
        target = (
            await infra.registry.get(scope, name) if name else await infra.active(scope_key=scope)
        )
        if target is None:
            await interaction.response.send_message("No such target.", ephemeral=True)
            return
        lines = [
            f"**{target.name}**",
            f"provider: `{target.provider}`",
            f"status: `{target.status}`",
            f"external id: `{target.external_id}`",
        ]
        if target.url:
            lines.append(f"url: {target.url}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @group.command(name="logs", description="Retrieve logs for a target.")
    @app_commands.describe(name="Target name (defaults to the active target).")
    async def logs(self, interaction: discord.Interaction, name: Optional[str] = None) -> None:
        if not await self._require_owner(interaction):
            return
        await interaction.response.send_message(
            "Log retrieval is not wired yet. Render logs need the per-service log "
            "endpoint, and VM logs require a worker job id (use `/vm exec` and read the "
            "returned output).",
            ephemeral=True,
        )

    @group.command(name="exec", description="Run a command on a registered VM worker.")
    @app_commands.describe(name="Registered VM target name.", command="Command line, e.g. `pytest -q`.")
    async def exec(self, interaction: discord.Interaction, name: str, command: str) -> None:
        if not await self._require_owner(interaction):
            return
        runtime = getattr(self.chatbot, "agent_runtime", None)
        if runtime is None or runtime.worker is None:
            await interaction.response.send_message(
                "No VM worker is configured (set WORKER_URL and WORKER_TOKEN).", ephemeral=True
            )
            return
        scope = scope_key_for(interaction.guild_id)
        target = await runtime.targets.get(scope_key=scope, kind="vm", name=name)
        if target is None:
            await interaction.response.send_message(
                f"`{name}` is not a registered VM. Register it with `/infrastructure register`.",
                ephemeral=True,
            )
            return
        decision = await runtime.permissions.check(
            principal_id=interaction.user.id,
            scope=PermissionScope.VM_EXEC,
            resource_type=ResourceType.VM,
            resource_id=name,
        )
        if not decision.allowed:
            await interaction.response.send_message(
                f"Not permitted: {decision.reason}", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            result = await runtime.worker.run_command(shlex.split(command))
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        output = (result.stdout or result.stderr or "").strip()[-1500:] or "(no output)"
        await interaction.followup.send(
            f"`{name}` → **{result.status.value}** (exit {result.exit_code})\n```\n{output}\n```"
        )

    @group.command(name="swap", description="Choose which target deploys go to.")
    @app_commands.describe(name="Target to make active.")
    async def swap(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._require_owner(interaction):
            return
        try:
            target = await self._require_infra().swap(
                scope_key=scope_key_for(interaction.guild_id), name=name
            )
        except InfraError as exc:
            await interaction.response.send_message(f"❌ {exc.user_message}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🔀 Active target is now `{target.name}` ({target.provider}).", ephemeral=True
        )

    @group.command(name="destroy", description="Destroy a target (irreversible).")
    @app_commands.describe(name="Target to destroy.", confirm="Must be true to actually destroy.")
    async def destroy(
        self, interaction: discord.Interaction, name: str, confirm: bool = False
    ) -> None:
        if not await self._require_owner(interaction):
            return
        try:
            destroyed = await self._require_infra().destroy(
                scope_key=scope_key_for(interaction.guild_id), name=name, confirm=confirm
            )
        except InfraError as exc:
            await interaction.response.send_message(f"❌ {exc.user_message}", ephemeral=True)
            return
        await interaction.response.send_message(f"🗑️ Destroyed `{destroyed}`.", ephemeral=True)

    @group.command(
        name="deploy",
        description="Publish code to the active target and deploy it.",
    )
    @app_commands.describe(
        target="Target to deploy to (defaults to the active target).",
        path="How the code reaches the target.",
        prompt="Optional: ask the AI to generate the code first.",
        attachment="Optional: a source file or ZIP of generated code to deploy.",
    )
    @app_commands.choices(path=PATH_CHOICES)
    async def deploy(
        self,
        interaction: discord.Interaction,
        path: str = "git",
        target: Optional[str] = None,
        prompt: Optional[str] = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        infra = self._require_infra()
        await interaction.response.defer(ephemeral=False, thinking=True)

        scope = scope_key_for(interaction.guild_id)
        try:
            files, source = await self._collect_files(interaction, prompt, attachment)
        except InfraError as exc:
            await interaction.followup.send(f"❌ {exc.user_message}", ephemeral=True)
            return

        if not files:
            await interaction.followup.send(
                "❌ Nothing to deploy. Provide a `prompt` to generate code or attach a file/ZIP.",
                ephemeral=True,
            )
            return

        try:
            result = await infra.deploy(
                scope_key=scope,
                name=target,
                path=DeployPath.parse(path),
                files=files,
                message=f"Deploy from Discord by {interaction.user.display_name}",
            )
        except InfraError as exc:
            await interaction.followup.send(f"❌ {exc.user_message}", ephemeral=True)
            return

        summary = (
            f"🚀 Deploy requested for `{result.target}` ({len(files)} file(s), from {source}).\n"
            f"{result.notes or ''}"
        )
        if result.url:
            summary += f"\n{result.url}"
        await interaction.followup.send(summary, ephemeral=False)

    # -- helpers ------------------------------------------------------------
    async def _collect_files(
        self,
        interaction: discord.Interaction,
        prompt: Optional[str],
        attachment: Optional[discord.Attachment],
    ) -> tuple[dict[str, str], str]:
        """Gather deployable text files from an attachment or an AI generation."""

        if attachment is not None:
            if attachment.size > self.config.files.max_archive_bytes:
                raise InfraError("That attachment is too large to deploy.")
            try:
                data = await attachment.read()
            except discord.HTTPException as exc:
                raise InfraError("Could not download that attachment.") from exc
            return self._expand([(attachment.filename, data)]), f"`{attachment.filename}`"

        if prompt:
            if self.engine is None:
                raise InfraError("Code generation is not available.")
            from engine import IncomingRequest

            response = await self.engine.handle(
                IncomingRequest(
                    user_id=interaction.user.id,
                    user_name=getattr(interaction.user, "display_name", interaction.user.name),
                    prompt=f"Produce the complete deployable project as files. {prompt}",
                    guild_id=interaction.guild_id,
                    channel_id=interaction.channel_id,
                    kind="channel" if interaction.guild_id else "dm",
                    is_owner=True,
                )
            )
            if not response.files:
                raise InfraError(
                    "The AI did not produce any files to deploy. Try a more explicit prompt."
                )
            return self._expand([(f.filename, f.data) for f in response.files]), "AI generation"

        return {}, "nothing"

    def _expand(self, payloads: list[tuple[str, bytes]]) -> dict[str, str]:
        return expand_payloads(payloads, self.config.files)
