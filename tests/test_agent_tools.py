"""Agent capability tests.

Covers the capabilities added for the autonomy requirements: authorized GitHub
Actions dispatch, routine service restarts, VM inspection/log retrieval, and the
database-backed resolution of a production deploy scope (``auto_prod``).
"""

from __future__ import annotations

import pytest

from agent.models import ToolResult
from agent.orchestrator import TaskOrchestrator
from agent.tools import ToolContext, _resolve_deploy_scope
from github.client import DryRunGitHubIntegration
from permissions.models import PermissionScope, ResourceType

from tests.conftest import OWNER_ID

SCOPE = "g1"


class FakeWorker:
    """Minimal stand-in for the VM worker client."""

    def __init__(self, stdout: str = "", stderr: str = "", fail: bool = False) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._fail = fail
        self.requests: list[str] = []

    async def logs(self, job_id: str):
        self.requests.append(job_id)
        if self._fail:
            raise RuntimeError("worker offline")
        return self._stdout, self._stderr


def context(**overrides) -> ToolContext:
    base = dict(task_id=1, principal_id=OWNER_ID, dry_run=True)
    base.update(overrides)
    return ToolContext(**base)


async def run_tool(name: str, args: dict, ctx: ToolContext) -> ToolResult:
    from agent.tools import default_registry

    registry = default_registry()
    spec = registry.get(name)
    assert spec is not None, f"tool {name} is not registered"
    return await registry.execute(spec, args, ctx)


# --------------------------------------------------------------------- actions
async def test_registered_toolset_includes_new_capabilities():
    from agent.tools import default_registry

    names = set(default_registry().names())
    assert {
        "actions.dispatch",
        "render.restart",
        "vm.inspect",
        "vm.logs",
        "render.deploy",
        "worker.run",
        "surge.publish",
    } <= names


async def test_actions_dispatch_requires_repo_workflow_ref():
    github = DryRunGitHubIntegration()
    result = await run_tool("actions.dispatch", {"repo": "owner/repo"}, context(github=github))
    assert result.ok is False
    assert "required" in result.error
    assert github.calls == []


async def test_actions_dispatch_triggers_workflow():
    github = DryRunGitHubIntegration()
    result = await run_tool(
        "actions.dispatch",
        {"repo": "owner/repo", "workflow": "ci.yml", "ref": "main", "inputs": {"env": "test"}},
        context(github=github),
    )
    assert result.ok is True
    assert ("dispatch_workflow", ("owner/repo", "ci.yml", "main", (("env", "test"),))) in github.calls


async def test_actions_dispatch_without_github_fails_honestly():
    result = await run_tool(
        "actions.dispatch", {"repo": "o/r", "workflow": "ci.yml", "ref": "main"}, context()
    )
    assert result.ok is False
    assert "GitHub" in result.error


# --------------------------------------------------------------------- restart
async def test_render_restart_targets_the_named_service(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="app")
    result = await run_tool(
        "render.restart", {"service": "app"}, context(infra=infra_service, scope_key=SCOPE)
    )
    assert result.ok is True
    assert "app" in result.summary


async def test_render_logs_returns_lines(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="app")
    result = await run_tool(
        "render.logs", {"service": "app"}, context(infra=infra_service, scope_key=SCOPE)
    )
    assert result.ok is True
    assert "dry-run log line" in result.data.get("logs", "")


async def test_render_logs_without_plane_fails_honestly():
    result = await run_tool("render.logs", {}, context())
    assert result.ok is False


async def test_render_status_reports_deploy_states(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="app")
    result = await run_tool(
        "render.status", {"service": "app"}, context(infra=infra_service, scope_key=SCOPE)
    )
    assert result.ok is True
    assert "live" in result.summary


async def test_render_restart_without_a_target_is_refused(infra_service):
    result = await run_tool(
        "render.restart", {"service": "ghost"}, context(infra=infra_service, scope_key=SCOPE)
    )
    assert result.ok is False


async def test_render_restart_without_plane_fails_honestly():
    result = await run_tool("render.restart", {"service": "app"}, context())
    assert result.ok is False
    assert "control plane" in result.error


# -------------------------------------------------------------------------- vm
async def test_vm_inspect_lists_registered_targets(trusted_targets):
    await trusted_targets.register(
        scope_key=SCOPE, kind="vm", name="buildbox", config={"host": "10.0.0.5"}
    )
    result = await run_tool("vm.inspect", {}, context(targets=trusted_targets, scope_key=SCOPE))
    assert result.ok is True
    assert "buildbox" in result.summary and "10.0.0.5" in result.summary


async def test_vm_inspect_reports_when_none_registered(trusted_targets):
    result = await run_tool("vm.inspect", {}, context(targets=trusted_targets, scope_key=SCOPE))
    assert result.ok is False
    assert "No VM targets" in result.error


async def test_vm_logs_reads_worker_output():
    worker = FakeWorker(stdout="built ok", stderr="")
    result = await run_tool("vm.logs", {"job": "job-1"}, context(worker=worker))
    assert result.ok is True
    assert result.data.get("stdout") == "built ok"
    assert worker.requests == ["job-1"]


async def test_vm_logs_requires_a_job_id():
    result = await run_tool("vm.logs", {}, context(worker=FakeWorker()))
    assert result.ok is False


async def test_vm_logs_without_worker_fails_honestly():
    result = await run_tool("vm.logs", {"job": "j"}, context())
    assert result.ok is False
    assert "worker" in result.error


# -------------------------------------------------------------- deploy scoping
async def test_scope_resolver_upgrades_only_auto_prod_targets(permission_service):
    await permission_service.grant(
        principal_id=OWNER_ID,
        scope=PermissionScope.DEPLOY_PROD,
        resource_type=ResourceType.SERVICE,
        resource_id="prod-app",
        actor_id=OWNER_ID,
        auto_prod=True,
    )
    upgraded = await _resolve_deploy_scope(permission_service, OWNER_ID, {"service": "prod-app"})
    assert upgraded is PermissionScope.DEPLOY_PROD
    # An unrelated service is not preauthorized for production.
    assert await _resolve_deploy_scope(permission_service, OWNER_ID, {"service": "dev-app"}) is None
    # No service in the args means no upgrade.
    assert await _resolve_deploy_scope(permission_service, OWNER_ID, {}) is None


async def test_scope_resolver_requires_auto_prod_flag(permission_service):
    await permission_service.grant(
        principal_id=OWNER_ID,
        scope=PermissionScope.DEPLOY_PROD,
        resource_type=ResourceType.SERVICE,
        resource_id="app",
        actor_id=OWNER_ID,
        auto_prod=False,
    )
    assert await _resolve_deploy_scope(permission_service, OWNER_ID, {"service": "app"}) is None


async def test_orchestrator_applies_scope_resolver(
    config, task_store, permission_service, approval_service
):
    """A non-owner with a prod auto-grant passes; without it, the step is blocked."""

    from agent.models import Task, TaskKind, TaskStatus, TaskStep
    from agent.tools import ToolRegistry, ToolSpec

    async def handler(_ctx, _args):
        return ToolResult.success("deployed")

    async def resolver(permissions, principal_id, args):
        return await _resolve_deploy_scope(permissions, principal_id, args)

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="render.deploy",
            description="deploy",
            handler=handler,
            scope=PermissionScope.DEPLOY_DEV,
            resource_type=ResourceType.SERVICE,
            scope_resolver=resolver,
        )
    )
    orchestrator = TaskOrchestrator(
        config=config,
        store=task_store,
        tools=tools,
        permissions=permission_service,
        approvals=approval_service,
    )

    requester = 4242

    async def make_task() -> Task:
        task = await task_store.create(
            Task(
                requester_id=requester,
                prompt="deploy",
                kind=TaskKind.DEPLOY_PROD,
                max_tool_calls=5,
                max_attempts=1,
            )
        )
        await task_store.add_steps(
            task.id,
            [
                TaskStep(
                    task_id=task.id,
                    step_index=0,
                    name="Deploy",
                    tool="render.deploy",
                    args={"service": "prod-app"},
                )
            ],
        )
        return await task_store.get(task.id)

    # Without the auto-prod grant the resolver still demands DEPLOY_PROD -> blocked.
    blocked = await orchestrator.run((await make_task()).id)
    assert blocked.status is TaskStatus.FAILED
    assert "Not permitted" in blocked.error

    # With the database grant, the resolved scope is satisfied.
    await permission_service.grant(
        principal_id=requester,
        scope=PermissionScope.DEPLOY_PROD,
        resource_type=ResourceType.SERVICE,
        resource_id="prod-app",
        actor_id=OWNER_ID,
        auto_prod=True,
    )
    passed = await orchestrator.run((await make_task()).id)
    assert passed.status is TaskStatus.SUCCEEDED


async def test_owner_always_authorized_regardless_of_resolver(
    config, task_store, permission_service, approval_service
):
    from agent.models import Task, TaskKind, TaskStatus, TaskStep
    from agent.tools import ToolRegistry, ToolSpec

    async def handler(_ctx, _args):
        return ToolResult.success("deployed")

    async def resolver(_permissions, _principal_id, _args):
        return PermissionScope.DEPLOY_PROD

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="deploy",
            description="",
            handler=handler,
            scope=PermissionScope.DEPLOY_DEV,
            scope_resolver=resolver,
        )
    )
    orchestrator = TaskOrchestrator(
        config=config,
        store=task_store,
        tools=tools,
        permissions=permission_service,
        approvals=approval_service,
    )
    task = await task_store.create(
        Task(requester_id=OWNER_ID, prompt="x", kind=TaskKind.DEPLOY_PROD, max_tool_calls=5)
    )
    await task_store.add_steps(
        task.id,
        [TaskStep(task_id=task.id, step_index=0, name="Deploy", tool="deploy", args={})],
    )
    result = await orchestrator.run(task.id)
    assert result.status is TaskStatus.SUCCEEDED
