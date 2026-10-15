"""Agent runtime: constructs and holds the wired agent subsystems."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from agent.orchestrator import TaskOrchestrator
from agent.store import TaskStore
from agent.targets import TrustedTargetStore
from agent.tools import ToolRegistry, default_registry
from approvals.service import ApprovalService
from config import Config
from database.database import Database
from github.client import GitHubIntegration, build_github
from infra.service import InfraService
from infra.surge import SurgePublisher, build_surge
from permissions.service import PermissionService
from permissions.store import PermissionStore
from worker.client import WorkerClient

logger = logging.getLogger(__name__)


@dataclass
class AgentRuntime:
    config: Config
    permissions: PermissionService
    approvals: ApprovalService
    targets: TrustedTargetStore
    tasks: TaskStore
    tools: ToolRegistry
    orchestrator: TaskOrchestrator
    github: Optional[GitHubIntegration] = None
    worker: Optional[WorkerClient] = None
    infra: Optional[InfraService] = None
    surge: Optional[SurgePublisher] = None

    @property
    def github_method(self) -> str:
        return self.github.method if self.github else "unconfigured"

    def integration_summary(self) -> str:
        return (
            f"github: {self.github_method} · "
            f"worker: {'configured' if self.worker else 'unconfigured'} · "
            f"surge: {'ready' if self.surge else 'unconfigured'} · "
            f"dry-run: {'on' if self.config.agent.dry_run else 'OFF'}"
        )

    async def close(self) -> None:
        for integration in (self.github, self.worker):
            if integration is not None:
                try:
                    await integration.close()
                except Exception:  # pragma: no cover - best effort
                    logger.warning("Failed to close %s cleanly", type(integration).__name__)


def build_agent_runtime(
    config: Config, db: Database, *, infra: Optional[InfraService] = None
) -> AgentRuntime:
    permissions = PermissionService(config, PermissionStore(db))
    approvals = ApprovalService(config, db)
    targets = TrustedTargetStore(db)
    tasks = TaskStore(db)

    github = build_github(config)

    worker: Optional[WorkerClient] = None
    if config.agent.worker_url and config.agent.worker_token:
        try:
            worker = WorkerClient(
                config.agent.worker_url,
                config.agent.worker_token,
                timeout=config.agent.worker_timeout,
            )
        except Exception as exc:  # pragma: no cover - configuration guard
            logger.warning("VM worker disabled: %s", exc)

    surge = build_surge(config)
    tools = default_registry()
    orchestrator = TaskOrchestrator(
        config=config,
        store=tasks,
        tools=tools,
        permissions=permissions,
        approvals=approvals,
        github=github,
        worker=worker,
        infra=infra,
        surge=surge,
        targets=targets,
    )

    return AgentRuntime(
        config=config,
        permissions=permissions,
        approvals=approvals,
        targets=targets,
        tasks=tasks,
        tools=tools,
        orchestrator=orchestrator,
        github=github,
        worker=worker,
        infra=infra,
        surge=surge,
    )
