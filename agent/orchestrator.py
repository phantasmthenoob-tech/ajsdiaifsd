"""Durable autonomous task orchestrator.

The orchestrator is the only component allowed to decide that work happened. It:

* checks permissions *before* each tool call (from the database, never from model
  text),
* pauses destructive steps for owner approval,
* enforces a per-task deadline, tool-call budget and retry limit,
* records every step outcome durably, so a restart resumes rather than forgets,
* reports only what the tools actually returned.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from agent.models import (
    StepStatus,
    Task,
    TaskKind,
    TaskPlan,
    TaskStatus,
    TaskStep,
    ToolResult,
)
from agent.planner import PlanContext, build_plan
from agent.store import TaskStore
from agent.tools import ToolContext, ToolRegistry
from approvals.service import ApprovalService, ApprovalStatus
from config import Config
from permissions.models import PermissionScope, ResourceType
from permissions.service import PermissionService
from utils import utcnow_iso

logger = logging.getLogger(__name__)


class AgentError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class TaskOrchestrator:
    def __init__(
        self,
        *,
        config: Config,
        store: TaskStore,
        tools: ToolRegistry,
        permissions: PermissionService,
        approvals: ApprovalService,
        github: Optional[Any] = None,
        worker: Optional[Any] = None,
        infra: Optional[Any] = None,
        surge: Optional[Any] = None,
        targets: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.store = store
        self.tools = tools
        self.permissions = permissions
        self.approvals = approvals
        self.github = github
        self.worker = worker
        self.infra = infra
        self.surge = surge
        self.targets = targets

    # -- submission ---------------------------------------------------------
    async def submit(
        self,
        *,
        prompt: str,
        requester_id: int,
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        context: Optional[PlanContext] = None,
    ) -> Task:
        plan: TaskPlan = build_plan(prompt, context=context)
        deadline = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self.config.agent.task_deadline_seconds)
        ).replace(microsecond=0).isoformat()

        task = Task(
            requester_id=requester_id,
            prompt=prompt,
            kind=plan.kind,
            guild_id=guild_id,
            channel_id=channel_id,
            plan_summary=plan.summary,
            max_tool_calls=self.config.agent.max_tool_calls,
            max_attempts=self.config.agent.max_step_attempts,
            deadline=deadline,
        )
        await self.store.create(task)
        steps = [
            TaskStep(
                task_id=task.id,
                step_index=index,
                name=planned.name,
                tool=planned.tool,
                args=planned.args,
            )
            for index, planned in enumerate(plan.steps)
        ]
        await self.store.add_steps(task.id, steps)
        task.steps = steps
        return task

    # -- execution ----------------------------------------------------------
    async def run(self, task_id: int) -> Task:
        task = await self.store.get(task_id)
        if task is None:
            raise AgentError(f"There is no task #{task_id}.")
        if task.is_terminal:
            return task

        await self.store.update_task(task_id, status=TaskStatus.RUNNING)
        task.status = TaskStatus.RUNNING

        scope_key = f"g{task.guild_id}" if task.guild_id else "owner"
        context = ToolContext(
            task_id=task_id,
            principal_id=task.requester_id,
            dry_run=self.config.agent.dry_run,
            scope_key=scope_key,
            github=self.github,
            worker=self.worker,
            infra=self.infra,
            surge=self.surge,
            targets=self.targets,
        )

        for step in task.steps:
            if step.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED):
                continue

            current = await self.store.get(task_id, with_steps=False)
            if current is None or current.status is TaskStatus.CANCELLED:
                return await self._load(task_id)
            if self._deadline_passed(current):
                await self._fail(task_id, "Task deadline exceeded before completion.")
                return await self._load(task_id)

            spec = self.tools.get(step.tool) if step.tool else None
            if spec is None:
                await self.store.update_step(step.id, status=StepStatus.FAILED, error="unknown tool")
                await self._fail(task_id, f"Unknown tool `{step.tool}` in the plan.")
                return await self._load(task_id)

            # 1) Authorization (database-backed only).
            scope = step.args.get("_scope_override") or spec.scope
            if spec.scope_resolver is not None:
                try:
                    resolved = await spec.scope_resolver(
                        self.permissions, task.requester_id, step.args
                    )
                except Exception:  # pragma: no cover - never breaks the task loop
                    resolved = None
                if resolved is not None:
                    scope = resolved
            if scope is not None:
                resource_id = str(
                    step.args.get("repo")
                    or step.args.get("service")
                    or step.args.get("domain")
                    or step.args.get("name")
                    or spec.resource_type.value
                )
                decision = await self.permissions.check(
                    principal_id=task.requester_id,
                    scope=scope,
                    resource_type=spec.resource_type,
                    resource_id=resource_id,
                )
                if not decision.allowed:
                    await self.store.update_step(step.id, status=StepStatus.BLOCKED, error=decision.reason)
                    await self._fail(
                        task_id,
                        f"Not permitted to run `{spec.name}` ({decision.reason}).",
                    )
                    return await self._load(task_id)

            # 2) Approval gate for destructive operations.
            if spec.destructive:
                pause = await self._ensure_approval(task, step, spec)
                if pause is not None:
                    return await self._load(task_id)

            # 3) Budget.
            calls = await self.store.increment_tool_calls(task_id)
            if calls > task.max_tool_calls:
                await self._fail(task_id, f"Tool-call budget exceeded ({task.max_tool_calls}).")
                return await self._load(task_id)

            # 4) Execute with bounded retries.
            result = await self._execute(step, spec, context, max_attempts=task.max_attempts)
            ok = result.ok or self._optional(step)
            if not ok:
                await self._fail(task_id, result.error or result.summary)
                return await self._load(task_id)
            await self.store.update_step(step.id, status=StepStatus.SUCCEEDED, result=result.summary)

        await self._succeed(task_id)
        return await self._load(task_id)

    async def cancel(self, task_id: int) -> Task:
        task = await self.store.get(task_id)
        if task is None:
            raise AgentError(f"There is no task #{task_id}.")
        if not task.is_terminal:
            await self.store.update_task(task_id, status=TaskStatus.CANCELLED, result="Cancelled by owner.")
        return await self._load(task_id)

    async def recover(self) -> list[int]:
        """Reset tasks orphaned by a restart so they can be resumed."""

        ids = await self.store.recover_orphans()
        if ids:
            logger.info("Recovered %d interrupted task(s): %s", len(ids), ids)
        return ids

    async def status(self, task_id: int) -> Optional[Task]:
        return await self.store.get(task_id)

    async def active_tasks(self, *, limit: int = 20) -> list[Task]:
        return await self.store.list_by_status(
            [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL], limit=limit
        )

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _optional(step: TaskStep) -> bool:
        return bool(step.args.get("_optional"))

    def _deadline_passed(self, task: Task) -> bool:
        return bool(task.deadline) and task.deadline <= utcnow_iso()

    async def _ensure_approval(self, task: Task, step: TaskStep, spec: Any) -> Optional[str]:
        target = f"{spec.name}:{step.args.get('repo') or step.args.get('service') or step.args.get('domain') or '*'}"
        if task.approval_id:
            approval = await self.approvals.get(task.approval_id)
            if approval and approval.status is ApprovalStatus.APPROVED:
                if await self.approvals.consume(task.approval_id, operation=spec.name, target=target):
                    await self.store.update_task(task.id, approval_id=None)
                    task.approval_id = None
                    return None
            if approval and approval.status in (ApprovalStatus.DENIED, ApprovalStatus.EXPIRED):
                await self._fail(task.id, f"Approval #{task.approval_id} was {approval.status.value}.")
                return "denied"
            return "awaiting"

        approval = await self.approvals.request(
            operation=spec.name,
            target=target,
            requested_by=task.requester_id,
            task_id=task.id,
            reason=step.name,
        )
        await self.store.update_task(
            task.id, status=TaskStatus.AWAITING_APPROVAL, approval_id=approval.id
        )
        logger.info("Task #%s paused awaiting approval #%s", task.id, approval.id)
        return "requested"

    async def _execute(
        self, step: TaskStep, spec: Any, context: ToolContext, *, max_attempts: int
    ) -> ToolResult:
        attempts = step.attempts or 0
        while True:
            attempts += 1
            await self.store.update_step(step.id, status=StepStatus.RUNNING, attempts=attempts)
            result = await self.tools.execute(spec, step.args, context)
            if result.ok:
                return result
            if not result.retryable or attempts >= max_attempts:
                await self.store.update_step(
                    step.id, status=StepStatus.FAILED, attempts=attempts, error=result.error
                )
                return result
            logger.info(
                "Step %s failed (attempt %d/%d); retrying", spec.name, attempts, max_attempts
            )

    async def _fail(self, task_id: int, error: str) -> None:
        await self.store.update_task(task_id, status=TaskStatus.FAILED, error=error)

    async def _succeed(self, task_id: int) -> None:
        task = await self.store.get(task_id)
        if task is None:
            return
        lines = [
            f"- {step.name}: {step.result or step.status.value}"
            for step in task.steps
            if step.status is StepStatus.SUCCEEDED
        ]
        result = "Completed:\n" + "\n".join(lines) if lines else "Completed with no steps recorded."
        await self.store.update_task(task_id, status=TaskStatus.SUCCEEDED, result=result)

    async def _load(self, task_id: int) -> Task:
        task = await self.store.get(task_id)
        assert task is not None
        return task


__all__ = ["TaskOrchestrator", "AgentError", "TaskKind", "PermissionScope", "ResourceType"]
