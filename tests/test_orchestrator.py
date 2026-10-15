"""Orchestrator tests.

These verify the core autonomy guarantees: permission is checked from the
database, destructive work pauses for approval, failures are retried within
bounds, budgets/deadlines stop runaway work, and state survives a restart.
"""

from __future__ import annotations

from typing import Optional

from agent.models import Task, TaskKind, TaskStatus, TaskStep, ToolResult
from agent.orchestrator import TaskOrchestrator
from agent.tools import ToolRegistry, ToolSpec
from permissions.models import PermissionScope, ResourceType

from tests.conftest import OWNER_ID

PAST = "2000-01-01T00:00:00+00:00"


def registry_with(specs: dict) -> ToolRegistry:
    """specs: name -> (ToolResult | callable, scope, destructive)."""

    registry = ToolRegistry()

    def build(name, result, scope, destructive):
        async def handler(_ctx, _args, _r=result):
            return _r() if callable(_r) else _r

        registry.register(
            ToolSpec(
                name=name,
                description="test tool",
                handler=handler,
                scope=scope,
                destructive=destructive,
            )
        )

    for name, (result, scope, destructive) in specs.items():
        build(name, result, scope, destructive)
    return registry


async def make_task(store, steps, **overrides) -> Task:
    defaults = dict(
        requester_id=OWNER_ID,
        prompt="do the thing",
        kind=TaskKind.ANSWER,
        max_tool_calls=10,
        max_attempts=2,
    )
    defaults.update(overrides)
    task = await store.create(Task(**defaults))
    await store.add_steps(
        task.id,
        [
            TaskStep(task_id=task.id, step_index=i, name=name, tool=tool, args=args)
            for i, (name, tool, args) in enumerate(steps)
        ],
    )
    return await store.get(task.id)


def build_orchestrator(config, store, tools, permission_service, approval_service):
    return TaskOrchestrator(
        config=config,
        store=store,
        tools=tools,
        permissions=permission_service,
        approvals=approval_service,
    )


async def test_successful_task_records_real_step_results(
    config, task_store, permission_service, approval_service
):
    tools = registry_with(
        {
            "a": (ToolResult.success("did A", value=1), None, False),
            "b": (ToolResult.success("did B"), None, False),
        }
    )
    orchestrator = build_orchestrator(config, task_store, tools, permission_service, approval_service)
    task = await make_task(task_store, [("Step A", "a", {}), ("Step B", "b", {})])

    result = await orchestrator.run(task.id)
    assert result.status is TaskStatus.SUCCEEDED
    assert "did A" in result.result and "did B" in result.result
    assert all(step.status.value == "succeeded" for step in result.steps)


async def test_permission_denial_blocks_task(config, task_store, permission_service, approval_service):
    tools = registry_with(
        {
            "repo.op": (
                ToolResult.success("should not run"),
                PermissionScope.REPO_WRITE,
                False,
            )
        }
    )
    orchestrator = build_orchestrator(config, task_store, tools, permission_service, approval_service)
    task = await make_task(
        task_store, [("Write repo", "repo.op", {"repo": "owner/x"})], requester_id=4242
    )

    result = await orchestrator.run(task.id)
    assert result.status is TaskStatus.FAILED
    assert "Not permitted" in result.error
    assert result.steps[0].status.value == "blocked"


async def test_owner_bypasses_grant_checks(config, task_store, permission_service, approval_service):
    tools = registry_with(
        {"repo.op": (ToolResult.success("ran"), PermissionScope.REPO_WRITE, False)}
    )
    orchestrator = build_orchestrator(config, task_store, tools, permission_service, approval_service)
    task = await make_task(task_store, [("Write repo", "repo.op", {"repo": "owner/x"})])

    result = await orchestrator.run(task.id)
    assert result.status is TaskStatus.SUCCEEDED


async def test_destructive_step_pauses_then_resumes(
    config, task_store, permission_service, approval_service
):
    tools = registry_with(
        {"danger": (ToolResult.success("deployed"), PermissionScope.DEPLOY_PROD, True)}
    )
    orchestrator = build_orchestrator(config, task_store, tools, permission_service, approval_service)
    task = await make_task(
        task_store, [("Deploy", "danger", {"service": "svc"})], kind=TaskKind.DEPLOY_PROD
    )

    paused = await orchestrator.run(task.id)
    assert paused.status is TaskStatus.AWAITING_APPROVAL
    assert paused.approval_id is not None

    await approval_service.decide(paused.approval_id, actor_id=OWNER_ID, approve=True)

    resumed = await orchestrator.run(task.id)
    assert resumed.status is TaskStatus.SUCCEEDED


async def test_denied_approval_fails_the_task(
    config, task_store, permission_service, approval_service
):
    tools = registry_with(
        {"danger": (ToolResult.success("x"), PermissionScope.DEPLOY_PROD, True)}
    )
    orchestrator = build_orchestrator(config, task_store, tools, permission_service, approval_service)
    task = await make_task(task_store, [("Deploy", "danger", {"service": "svc"})])

    paused = await orchestrator.run(task.id)
    await approval_service.decide(paused.approval_id, actor_id=OWNER_ID, approve=False)
    result = await orchestrator.run(task.id)
    assert result.status is TaskStatus.FAILED
    assert "denied" in (result.error or "").lower()


async def test_retryable_failure_is_retried(config, task_store, permission_service, approval_service):
    calls = {"n": 0}

    async def flaky(_ctx, _args):
        calls["n"] += 1
        if calls["n"] < 2:
            return ToolResult.failure("transient", retryable=True)
        return ToolResult.success("recovered")

    tools = ToolRegistry()
    tools.register(ToolSpec(name="flaky", description="", handler=flaky))
    orchestrator = build_orchestrator(config, task_store, tools, permission_service, approval_service)
    task = await make_task(task_store, [("Flaky", "flaky", {})])

    result = await orchestrator.run(task.id)
    assert result.status is TaskStatus.SUCCEEDED
    assert calls["n"] == 2
    assert result.steps[0].attempts == 2


async def test_non_retryable_failure_stops_immediately(
    config, task_store, permission_service, approval_service
):
    tools = registry_with({"boom": (ToolResult.failure("permanent"), None, False)})
    orchestrator = build_orchestrator(config, task_store, tools, permission_service, approval_service)
    task = await make_task(task_store, [("Boom", "boom", {})])
    result = await orchestrator.run(task.id)
    assert result.status is TaskStatus.FAILED
    assert result.steps[0].attempts == 1


async def test_tool_budget_is_enforced(config, task_store, permission_service, approval_service):
    tools = registry_with(
        {
            "a": (ToolResult.success("a"), None, False),
            "b": (ToolResult.success("b"), None, False),
            "c": (ToolResult.success("c"), None, False),
        }
    )
    orchestrator = build_orchestrator(config, task_store, tools, permission_service, approval_service)
    task = await make_task(
        task_store, [("A", "a", {}), ("B", "b", {}), ("C", "c", {})], max_tool_calls=1
    )
    result = await orchestrator.run(task.id)
    assert result.status is TaskStatus.FAILED
    assert "budget" in (result.error or "").lower()


async def test_deadline_is_enforced(config, task_store, permission_service, approval_service):
    tools = registry_with({"a": (ToolResult.success("a"), None, False)})
    orchestrator = build_orchestrator(config, task_store, tools, permission_service, approval_service)
    task = await make_task(task_store, [("A", "a", {})], deadline=PAST)
    result = await orchestrator.run(task.id)
    assert result.status is TaskStatus.FAILED
    assert "deadline" in (result.error or "").lower()


async def test_unknown_tool_fails_cleanly(config, task_store, permission_service, approval_service):
    orchestrator = build_orchestrator(
        config, task_store, ToolRegistry(), permission_service, approval_service
    )
    task = await make_task(task_store, [("Missing", "nope", {})])
    result = await orchestrator.run(task.id)
    assert result.status is TaskStatus.FAILED
    assert "Unknown tool" in (result.error or "")


async def test_cancel_marks_task_cancelled(config, task_store, permission_service, approval_service):
    tools = registry_with({"a": (ToolResult.success("a"), None, False)})
    orchestrator = build_orchestrator(config, task_store, tools, permission_service, approval_service)
    task = await make_task(task_store, [("A", "a", {})])
    cancelled = await orchestrator.cancel(task.id)
    assert cancelled.status is TaskStatus.CANCELLED


async def test_recovery_resets_orphaned_running_tasks(
    config, task_store, permission_service, approval_service
):
    orchestrator = build_orchestrator(
        config, task_store, ToolRegistry(), permission_service, approval_service
    )
    task = await make_task(task_store, [("A", "a", {})])
    await task_store.update_task(task.id, status=TaskStatus.RUNNING)

    recovered = await orchestrator.recover()
    assert task.id in recovered
    reloaded = await task_store.get(task.id)
    assert reloaded.status is TaskStatus.PENDING


async def test_submit_builds_and_persists_a_plan(
    config, task_store, permission_service, approval_service
):
    orchestrator = build_orchestrator(
        config, task_store, ToolRegistry(), permission_service, approval_service
    )
    task = await orchestrator.submit(
        prompt="inspect owner/repo", requester_id=OWNER_ID, guild_id=1, channel_id=2
    )
    assert task.id is not None
    assert task.kind is TaskKind.REPO_INSPECT
    assert task.plan_summary
    stored = await task_store.get(task.id)
    assert stored.steps and stored.steps[-1].tool == "task.verify"
