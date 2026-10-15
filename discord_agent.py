"""Owner command surface for the autonomous developer agent.

Every command here is owner-only and ephemeral for administration. Autonomous
work runs through the durable orchestrator, so a task survives restarts and is
observable with ``/task status``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from infra.models import InfraError
from permissions.models import PermissionScope, ResourceType

logger = logging.getLogger(__name__)

KIND_CHOICES = [
    app_commands.Choice(name="vm", value="vm"),
    app_commands.Choice(name="domain", value="domain"),
    app_commands.Choice(name="service", value="service"),
]

SCOPE_CHOICES = [
    app_commands.Choice(name="repo_read", value="repo_read"),
    app_commands.Choice(name="repo_write", value="repo_write"),
    app_commands.Choice(name="vm_inspect", value="vm_inspect"),
    app_commands.Choice(name="vm_exec", value="vm_exec"),
    app_commands.Choice(name="deploy_dev", value="deploy_dev"),
    app_commands.Choice(name="deploy_prod", value="deploy_prod"),
]


class AgentCog(commands.Cog):
    """Owner-only groups for the autonomous agent."""

    infrastructure = app_commands.Group(
        name="infrastructure", description="Owner-only: agent infrastructure and permissions."
    )
    repo = app_commands.Group(name="repo", description="Owner-only: repository operations.")
    task = app_commands.Group(name="task", description="Autonomous task management.")
    render = app_commands.Group(name="render", description="Owner-only: Render services.")
    surge = app_commands.Group(name="surge", description="Owner-only: static publishing.")

    def __init__(self, chatbot) -> None:
        self.chatbot = chatbot
        self.config: Config = chatbot.config
        self.rt = chatbot.agent_runtime

    # -- authorization ------------------------------------------------------
    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if self.config.is_owner(interaction.user.id):
            return True
        command = interaction.command.qualified_name if interaction.command else "unknown"
        logger.warning("Denied owner-only command '%s' for user id %s", command, interaction.user.id)
        await interaction.response.send_message(
            "That command is restricted to the bot owner.", ephemeral=True
        )
        return False

    def _scope_key(self, interaction: discord.Interaction) -> str:
        return f"g{interaction.guild_id}" if interaction.guild_id else "owner"

    # ==================================================================
    # /infrastructure
    # ==================================================================
    @infrastructure.command(name="status", description="Show agent capabilities and integrations.")
    async def infra_status(self, interaction: discord.Interaction) -> None:
        if not await self._require_owner(interaction):
            return
        active = await self.rt.orchestrator.active_tasks()
        lines = [
            f"**Agent status**",
            self.rt.integration_summary(),
            f"active tasks: {len(active)}",
            f"authorized repos: {', '.join(self.config.agent.github_allowed_repos) or 'none'}",
            f"surge domains: {', '.join(self.config.agent.surge_allowed_domains) or 'none'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @infrastructure.command(name="permissions", description="List granted permission scopes.")
    async def infra_permissions(self, interaction: discord.Interaction) -> None:
        if not await self._require_owner(interaction):
            return
        grants = await self.rt.permissions.list_grants()
        if not grants:
            await interaction.response.send_message(
                "No explicit grants. The owner is implicitly authorized for all scopes.",
                ephemeral=True,
            )
            return
        lines = [
            f"`{g.scope.value}` → {g.resource_type.value}:{g.resource_id} "
            f"(principal {g.principal_id}{', auto-prod' if g.auto_prod else ''})"
            for g in grants
        ]
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @infrastructure.command(name="targets", description="List registered trusted targets.")
    @app_commands.choices(kind=KIND_CHOICES)
    async def infra_targets(
        self, interaction: discord.Interaction, kind: Optional[str] = None
    ) -> None:
        if not await self._require_owner(interaction):
            return
        targets = await self.rt.targets.list(kind=kind)
        if not targets:
            await interaction.response.send_message("No trusted targets registered.", ephemeral=True)
            return
        lines = [f"`{t.kind}` **{t.name}**" for t in targets]
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @infrastructure.command(name="register", description="Register a trusted target and grant access.")
    @app_commands.choices(kind=KIND_CHOICES, scope=SCOPE_CHOICES)
    @app_commands.describe(
        kind="Target kind.",
        name="Target name (e.g. vm host, domain, service).",
        scope="Permission scope to grant for this target.",
        auto_prod="Preauthorize automatic production deploys to this target.",
        detail="Optional config (e.g. url=https://...).",
    )
    async def infra_register(
        self,
        interaction: discord.Interaction,
        kind: str,
        name: str,
        scope: str,
        auto_prod: bool = False,
        detail: Optional[str] = None,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        config = {}
        if detail and "=" in detail:
            key, _, value = detail.partition("=")
            config[key.strip()] = value.strip()
        try:
            target = await self.rt.targets.register(
                scope_key=self._scope_key(interaction),
                kind=kind,
                name=name,
                config=config,
            )
            resource_type = {
                "vm": ResourceType.VM,
                "domain": ResourceType.DOMAIN,
                "service": ResourceType.SERVICE,
            }[kind]
            await self.rt.permissions.grant(
                principal_id=self.config.owner_id,
                scope=PermissionScope.parse(scope),
                resource_type=resource_type,
                resource_id=target.name,
                actor_id=interaction.user.id,
                auto_prod=auto_prod,
            )
        except Exception as exc:
            message = getattr(exc, "user_message", str(exc))
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Registered `{kind}` **{target.name}** with `{scope}`"
            + (" (auto-prod)" if auto_prod else ""),
            ephemeral=True,
        )

    @infrastructure.command(name="unregister", description="Remove a trusted target.")
    @app_commands.choices(kind=KIND_CHOICES)
    @app_commands.describe(kind="Target kind.", name="Target name.")
    async def infra_unregister(
        self, interaction: discord.Interaction, kind: str, name: str
    ) -> None:
        if not await self._require_owner(interaction):
            return
        removed = await self.rt.targets.unregister(
            scope_key=self._scope_key(interaction), kind=kind, name=name
        )
        await interaction.response.send_message(
            f"🗑️ Unregistered `{name}`." if removed else f"No `{kind}` target named `{name}`.",
            ephemeral=True,
        )

    # ==================================================================
    # /repo
    # ==================================================================
    @repo.command(name="list", description="List authorized repositories.")
    async def repo_list(self, interaction: discord.Interaction) -> None:
        if not await self._require_owner(interaction):
            return
        repos = self.config.agent.github_allowed_repos
        if not repos:
            await interaction.response.send_message(
                "No repositories are authorized. Add them to GITHUB_ALLOWED_REPOS.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "\n".join(f"• `{r}`" for r in repos), ephemeral=True
        )

    @repo.command(name="inspect", description="Inspect a repository.")
    @app_commands.describe(repository="owner/name")
    async def repo_inspect(self, interaction: discord.Interaction, repository: str) -> None:
        if not await self._require_owner(interaction):
            return
        if self.rt.github is None:
            await interaction.response.send_message("GitHub is not configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await self.rt.github.get_repo(repository)
            branches = await self.rt.github.list_branches(repository)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ {getattr(exc, 'user_message', str(exc))}", ephemeral=True
            )
            return
        names = ", ".join(b.name for b in branches[:15]) or "none"
        await interaction.followup.send(
            f"**{data.get('full_name', repository)}**\n"
            f"default branch: `{data.get('default_branch', 'unknown')}`\n"
            f"auth method: `{self.rt.github_method}`\n"
            f"branches: {names}",
            ephemeral=True,
        )

    @repo.command(name="actions", description="Run an authorized GitHub Actions workflow.")
    @app_commands.describe(
        repository="owner/name",
        workflow="Workflow file or id, e.g. ci.yml",
        ref="Branch or tag to run against",
    )
    async def repo_actions(
        self, interaction: discord.Interaction, repository: str, workflow: str, ref: str = "main"
    ) -> None:
        if not await self._require_owner(interaction):
            return
        if self.rt.github is None:
            await interaction.response.send_message("GitHub is not configured.", ephemeral=True)
            return
        if repository not in self.config.agent.github_allowed_repos:
            await interaction.response.send_message(
                f"`{repository}` is not in GITHUB_ALLOWED_REPOS.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.rt.github.dispatch_workflow(repository, workflow, ref)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ {getattr(exc, 'user_message', str(exc))}", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"▶️ Dispatched `{workflow}` on `{ref}` in `{repository}`.", ephemeral=True
        )

    @repo.command(name="pr", description="Open a pull request.")
    @app_commands.describe(repository="owner/name", head="Source branch", base="Target branch", title="PR title")
    async def repo_pr(
        self,
        interaction: discord.Interaction,
        repository: str,
        head: str,
        base: str = "main",
        title: str = "Agent change",
    ) -> None:
        if not await self._require_owner(interaction):
            return
        if self.rt.github is None:
            await interaction.response.send_message("GitHub is not configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pr = await self.rt.github.create_pull_request(repository, head, base, title)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ {getattr(exc, 'user_message', str(exc))}", ephemeral=True
            )
            return
        await interaction.followup.send(f"✅ Opened PR #{pr.number}: {pr.url}", ephemeral=True)

    # ==================================================================
    # /task
    # ==================================================================
    @task.command(name="run", description="Start an autonomous task.")
    @app_commands.describe(prompt="What you want done, e.g. 'fix the failing test in owner/repo'.")
    async def task_run(self, interaction: discord.Interaction, prompt: str) -> None:
        if not await self._require_owner(interaction):
            return
        await interaction.response.defer(thinking=True)
        task = await self.rt.orchestrator.submit(
            prompt=prompt,
            requester_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
        )
        asyncio.create_task(self._run_task(interaction, task.id))
        await interaction.followup.send(
            f"🧠 Task #{task.id} started (`{task.kind.value}`).\n{task.plan_summary}\n\n"
            f"Track it with `/task status id:{task.id}`."
        )

    async def _run_task(self, interaction: discord.Interaction, task_id: int) -> None:
        try:
            task = await self.rt.orchestrator.run(task_id)
        except Exception:
            logger.exception("Task #%s crashed", task_id)
            return
        channel = interaction.channel
        if channel is None:
            return
        try:
            await channel.send(self._format_task(task))
        except discord.HTTPException:
            logger.warning("Could not post results for task #%s", task_id)

    @staticmethod
    def _format_task(task) -> str:
        icon = {
            "succeeded": "✅",
            "failed": "❌",
            "cancelled": "🛑",
            "awaiting_approval": "⏳",
        }.get(task.status.value, "ℹ️")
        body = [f"{icon} Task #{task.id} — **{task.status.value}**"]
        if task.plan_summary:
            body.append(task.plan_summary)
        if task.result:
            body.append(task.result[:1200])
        if task.error:
            body.append(f"error: {task.error[:800]}")
        if task.status.value == "awaiting_approval" and task.approval_id:
            body.append(f"Approve with `/approve id:{task.approval_id}`")
        return "\n".join(body)[:1900]

    @task.command(name="status", description="Show a task and its steps.")
    @app_commands.describe(id="Task id")
    async def task_status(self, interaction: discord.Interaction, id: int) -> None:
        if not await self._require_owner(interaction):
            return
        task = await self.rt.orchestrator.status(id)
        if task is None:
            await interaction.response.send_message(f"No task #{id}.", ephemeral=True)
            return
        lines = [f"**Task #{task.id}** — `{task.status.value}`"]
        if task.plan_summary:
            lines.append(task.plan_summary)
        for step in task.steps:
            lines.append(f"  {step.status.value}: {step.name}")
        if task.result:
            lines.append(task.result[:600])
        if task.error:
            lines.append(f"error: {task.error[:600]}")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @task.command(name="cancel", description="Cancel a running task.")
    @app_commands.describe(id="Task id")
    async def task_cancel(self, interaction: discord.Interaction, id: int) -> None:
        if not await self._require_owner(interaction):
            return
        try:
            task = await self.rt.orchestrator.cancel(id)
        except Exception as exc:
            await interaction.response.send_message(
                f"❌ {getattr(exc, 'user_message', str(exc))}", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🛑 Task #{task.id} is now `{task.status.value}`.", ephemeral=True
        )

    # ==================================================================
    # /approve
    # ==================================================================
    @app_commands.command(name="approve", description="Owner only: approve or deny a pending operation.")
    @app_commands.describe(id="Approval request id", deny="Set true to deny instead of approve.")
    async def approve(
        self, interaction: discord.Interaction, id: int, deny: bool = False
    ) -> None:
        if not await self._require_owner(interaction):
            return
        try:
            approval = await self.rt.approvals.decide(
                id, actor_id=interaction.user.id, approve=not deny
            )
        except Exception as exc:
            await interaction.response.send_message(
                f"❌ {getattr(exc, 'user_message', str(exc))}", ephemeral=True
            )
            return
        verb = "approved" if approval.status.value == "approved" else "denied"
        note = ""
        if approval.task_id and not deny:
            note = f"\nResuming task #{approval.task_id} — run `/task status id:{approval.task_id}`."
        await interaction.response.send_message(
            f"✅ Approval #{id} {verb} for {approval.describe()}.{note}", ephemeral=True
        )

    # ==================================================================
    # /render and /surge
    # ==================================================================
    @render.command(name="services", description="List tracked Render services.")
    async def render_services(self, interaction: discord.Interaction) -> None:
        if not await self._require_owner(interaction):
            return
        infra = getattr(self.chatbot, "infra_service", None)
        if infra is None:
            await interaction.response.send_message("Control plane not configured.", ephemeral=True)
            return
        targets = await infra.list(scope_key=self._scope_key(interaction))
        if not targets:
            await interaction.response.send_message("No services tracked.", ephemeral=True)
            return
        await interaction.response.send_message(
            "\n".join(f"• `{t.name}` ({t.provider}, {t.status})" for t in targets), ephemeral=True
        )

    @render.command(name="logs", description="Retrieve recent service logs.")
    @app_commands.describe(service="Service name (defaults to the active target).")
    async def render_logs(
        self, interaction: discord.Interaction, service: Optional[str] = None
    ) -> None:
        if not await self._require_owner(interaction):
            return
        infra = getattr(self.chatbot, "infra_service", None)
        if infra is None:
            await interaction.response.send_message("Control plane not configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            target, lines = await infra.logs(
                scope_key=self._scope_key(interaction), name=service
            )
        except Exception as exc:
            await interaction.followup.send(
                f"❌ {getattr(exc, 'user_message', str(exc))}", ephemeral=True
            )
            return
        if not lines:
            await interaction.followup.send(
                f"`{target.name}` returned no log lines.", ephemeral=True
            )
            return
        body = "\n".join(str(line) for line in lines[-30:])
        await interaction.followup.send(
            f"**{target.name}** (last {min(len(lines), 30)} of {len(lines)} lines)\n"
            f"```\n{body[:1800]}\n```",
            ephemeral=True,
        )

    @render.command(name="deploy", description="Trigger a deployment for a service.")
    @app_commands.describe(service="Service name (defaults to the active target).")
    async def render_deploy(
        self, interaction: discord.Interaction, service: Optional[str] = None
    ) -> None:
        if not await self._require_owner(interaction):
            return
        infra = getattr(self.chatbot, "infra_service", None)
        if infra is None:
            await interaction.response.send_message("Control plane not configured.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            result = await infra.trigger_deploy(
                scope_key=self._scope_key(interaction), name=service
            )
        except InfraError as exc:
            await interaction.followup.send(f"❌ {exc.user_message}", ephemeral=True)
            return
        await interaction.followup.send(f"🚀 {result.notes or 'Deploy triggered.'}")

    @surge.command(name="deploy", description="Generate and publish a static site to Surge.")
    @app_commands.describe(domain="Authorized Surge domain", prompt="What the static site should do")
    async def surge_deploy(
        self, interaction: discord.Interaction, domain: str, prompt: str
    ) -> None:
        if not await self._require_owner(interaction):
            return
        if self.rt.surge is None:
            await interaction.response.send_message("Surge is not configured.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        from engine import IncomingRequest
        from discord_infra import expand_payloads

        response = await self.chatbot.engine.handle(
            IncomingRequest(
                user_id=interaction.user.id,
                user_name=getattr(interaction.user, "display_name", interaction.user.name),
                prompt=(
                    "Produce a single-page static site as files. It MUST include an "
                    f"`index.html`. {prompt}"
                ),
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                kind="channel" if interaction.guild_id else "dm",
                is_owner=True,
            )
        )
        files = expand_payloads([(f.filename, f.data) for f in response.files], self.config.files)
        if not files:
            await interaction.followup.send("❌ No files were generated to publish.", ephemeral=True)
            return
        try:
            result = await self.rt.surge.publish(domain=domain, files=files)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ {getattr(exc, 'user_message', str(exc))}", ephemeral=True
            )
            return
        await interaction.followup.send(f"🌐 {result.notes}\n{result.url}")
