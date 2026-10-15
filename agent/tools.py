"""Tool registry.

Every capability the agent can invoke is a registered :class:`ToolSpec` that
declares the permission scope it needs, whether it mutates state, and whether it
is destructive. The orchestrator checks permission *before* invoking a tool, and
success is taken from the structured :class:`ToolResult` the tool returns — never
from the model's own prose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from agent.models import ToolResult
from permissions.models import PermissionScope, ResourceType

logger = logging.getLogger(__name__)

Handler = Callable[["ToolContext", dict[str, Any]], Awaitable[ToolResult]]

# A resolver may upgrade a tool's *required* scope at call time from the database
# (never from model text) - e.g. a deploy to a target with an `auto_prod` grant.
ScopeResolver = Callable[[Any, int, dict[str, Any]], Awaitable[Optional[PermissionScope]]]


@dataclass
class ToolContext:
    """Everything a tool needs, injected rather than discovered globally."""

    task_id: int
    principal_id: int
    dry_run: bool = True
    scope_key: str = "owner"
    github: Optional[Any] = None
    worker: Optional[Any] = None
    infra: Optional[Any] = None
    surge: Optional[Any] = None
    targets: Optional[Any] = None


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Handler
    scope: Optional[PermissionScope] = None
    resource_type: ResourceType = ResourceType.GLOBAL
    mutating: bool = False
    destructive: bool = False
    scope_resolver: Optional[ScopeResolver] = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    async def execute(
        self, tool: ToolSpec | str, args: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        spec = self.get(tool) if isinstance(tool, str) else tool
        if spec is None:
            return ToolResult.failure(f"Unknown tool `{tool}`.")
        try:
            return await spec.handler(context, args or {})
        except Exception as exc:  # a tool must never crash the task loop
            logger.warning("Tool %s raised %s", spec.name, exc.__class__.__name__)
            return ToolResult.failure(
                f"`{spec.name}` raised {exc.__class__.__name__}: {exc}", retryable=True
            )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def _note(_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return ToolResult.success("Prepared a response", prompt=args.get("prompt", ""))


async def _verify(_ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    # Real verification happens in the orchestrator against recorded step results.
    return ToolResult.success("Final state verified")


async def _repo_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.github is None:
        return ToolResult.failure("No GitHub integration is configured.")
    repo = args.get("repo")
    if not repo:
        return ToolResult.failure("No repository was specified.")
    data = await ctx.github.get_repo(repo)
    return ToolResult.success(
        f"Read `{repo}` (default branch: {data.get('default_branch', 'unknown')})",
        repo=repo,
        default_branch=data.get("default_branch"),
    )


async def _repo_branch(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.github is None:
        return ToolResult.failure("No GitHub integration is configured.")
    repo = args.get("repo")
    branch = args.get("branch")
    if not (repo and branch):
        return ToolResult.failure("Both `repo` and `branch` are required.")
    base = args.get("base") or "main"
    sha = await ctx.github.get_branch_sha(repo, base)
    if sha is None:
        return ToolResult.failure(f"Base branch `{base}` was not found in `{repo}`.")
    info = await ctx.github.create_branch(repo, branch, sha)
    return ToolResult.success(f"Created branch `{branch}` from `{base}`", branch=info.name, sha=info.sha)


async def _repo_commit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.github is None:
        return ToolResult.failure("No GitHub integration is configured.")
    repo = args.get("repo")
    branch = args.get("branch")
    files = args.get("files")
    if not repo or not branch:
        return ToolResult.failure("Both `repo` and `branch` are required.")
    if not isinstance(files, dict) or not files:
        return ToolResult.failure(
            "No file contents were supplied; refusing to commit without a diff.", retryable=False
        )
    sha = await ctx.github.put_files(repo, branch, files, args.get("message", "Agent change"))
    return ToolResult.success(
        f"Committed {len(files)} file(s) to `{branch}`", commit=sha, files=sorted(files)
    )


async def _repo_pr(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.github is None:
        return ToolResult.failure("No GitHub integration is configured.")
    repo, head = args.get("repo"), args.get("head")
    if not (repo and head):
        return ToolResult.failure("Both `repo` and `head` are required.")
    pr = await ctx.github.create_pull_request(
        repo, head, args.get("base", "main"), args.get("title", "Agent change"), args.get("body", "")
    )
    return ToolResult.success(f"Opened PR #{pr.number}", url=pr.url, number=pr.number)


async def _render_services(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    if ctx.infra is None:
        return ToolResult.failure("The infrastructure control plane is not configured.")
    targets = await ctx.infra.list(scope_key=ctx.scope_key)
    return ToolResult.success(
        f"Found {len(targets)} tracked service(s)", services=[t.name for t in targets]
    )


async def _render_logs(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.infra is None:
        return ToolResult.failure("The infrastructure control plane is not configured.")
    try:
        target, lines = await ctx.infra.logs(
            scope_key=ctx.scope_key,
            name=args.get("service") or None,
            limit=int(args.get("limit") or 100),
        )
    except Exception as exc:
        return ToolResult.failure(getattr(exc, "user_message", str(exc)))
    if not lines:
        return ToolResult.success(f"No log lines were returned for `{target.name}`.")
    return ToolResult.success(
        f"Retrieved {len(lines)} log line(s) from `{target.name}`",
        logs="\n".join(str(line) for line in lines[-40:]),
    )


async def _render_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.infra is None:
        return ToolResult.failure("The infrastructure control plane is not configured.")
    try:
        target, records = await ctx.infra.deploys(
            scope_key=ctx.scope_key,
            name=args.get("service") or None,
            limit=int(args.get("limit") or 5),
        )
    except Exception as exc:
        return ToolResult.failure(getattr(exc, "user_message", str(exc)))
    if not records:
        return ToolResult.success(f"No deploys are recorded for `{target.name}`.")
    summary = ", ".join(
        f"{record.get('id', '?')}={record.get('status', 'unknown')}" for record in records[:5]
    )
    return ToolResult.success(f"Deploys for `{target.name}`: {summary}", deploys=records[:5])


async def _render_deploy(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.infra is None:
        return ToolResult.failure("The infrastructure control plane is not configured.")
    # Triggers a deploy of the currently published branch/image. Publishing new
    # code is a separate, explicit operation.
    service = args.get("service")
    result = await ctx.infra.trigger_deploy(scope_key=ctx.scope_key, name=service or None)
    return ToolResult.success(
        f"Deploy triggered for `{result.target}`", url=result.url, notes=result.notes
    )


async def _worker_run(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.worker is None:
        return ToolResult.failure(
            "No VM worker is configured (set WORKER_URL and WORKER_TOKEN)."
        )
    command = args.get("command")
    if not command:
        return ToolResult.failure("A `command` (list of argv strings) is required.")
    result = await ctx.worker.run_command(list(command), files=args.get("files"))
    if not result.ok:
        return ToolResult.failure(
            f"Command failed (exit {result.exit_code}): {result.stderr.strip()[:200]}",
            retryable=result.exit_code != 0,
        )
    return ToolResult.success(
        f"Command succeeded on the worker", exit_code=result.exit_code, output=result.stdout[-2000:]
    )


async def _surge_publish(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.surge is None:
        return ToolResult.failure("No Surge publisher is configured.")
    domain = args.get("domain")
    files = args.get("files")
    if not domain or not files:
        return ToolResult.failure("Both `domain` and `files` are required.")
    result = await ctx.surge.publish(domain=domain, files=files)
    return ToolResult.success(f"Published to {result.domain}", url=result.url, executed=result.executed)


async def _resolve_deploy_scope(
    permissions: Any, principal_id: int, args: dict[str, Any]
) -> Optional[PermissionScope]:
    """Require ``deploy_prod`` only when the target is preauthorized for prod.

    The decision comes from a database grant (``auto_prod`` on a ``deploy_prod``
    grant for that exact service), never from the plan or the request text.
    """

    service = args.get("service")
    if not service:
        return None
    try:
        allowed = await permissions.has_auto_prod(principal_id=principal_id, service=str(service))
    except Exception:  # pragma: no cover - a resolver must never break a task
        return None
    return PermissionScope.DEPLOY_PROD if allowed else None


async def _actions_dispatch(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.github is None:
        return ToolResult.failure("No GitHub integration is configured.")
    repo = args.get("repo")
    workflow = args.get("workflow")
    ref = args.get("ref")
    if not repo or not workflow or not ref:
        return ToolResult.failure("`repo`, `workflow` and `ref` are required.")
    try:
        await ctx.github.dispatch_workflow(
            repo, str(workflow), str(ref), args.get("inputs") or None
        )
    except Exception as exc:
        return ToolResult.failure(getattr(exc, "user_message", str(exc)), retryable=True)
    return ToolResult.success(f"Dispatched workflow `{workflow}` at `{ref}` in `{repo}`")


async def _render_restart(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.infra is None:
        return ToolResult.failure("The infrastructure control plane is not configured.")
    name = args.get("service")
    try:
        target = await ctx.infra.restart(scope_key=ctx.scope_key, name=name or None)
    except Exception as exc:
        return ToolResult.failure(getattr(exc, "user_message", str(exc)), retryable=True)
    return ToolResult.success(f"Restarted `{target.name}` ({target.provider}).")


async def _vm_inspect(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.targets is None:
        return ToolResult.failure("No trusted-target registry is available.")
    name = args.get("name")
    if name:
        target = await ctx.targets.get(scope_key=ctx.scope_key, kind="vm", name=str(name))
        targets = [target] if target is not None else []
    else:
        targets = await ctx.targets.list(scope_key=ctx.scope_key, kind="vm")
    if not targets:
        return ToolResult.failure("No VM targets are registered.")
    lines = [
        f"{t.name} @ {t.config.get('host', 'host not recorded')}" for t in targets if t is not None
    ]
    worker = "configured" if ctx.worker else "unconfigured"
    return ToolResult.success(
        f"{len(lines)} VM target(s); worker {worker}: " + ", ".join(lines)
    )


async def _vm_logs(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.worker is None:
        return ToolResult.failure("No VM worker is configured (set WORKER_URL and WORKER_TOKEN).")
    job_id = args.get("job")
    if not job_id:
        return ToolResult.failure("A `job` id is required.")
    try:
        stdout, stderr = await ctx.worker.logs(str(job_id))
    except Exception as exc:
        return ToolResult.failure(getattr(exc, "user_message", str(exc)), retryable=True)
    return ToolResult.success(
        f"Logs for job `{job_id}`", stdout=stdout[-2000:], stderr=stderr[-2000:]
    )


def default_registry() -> ToolRegistry:
    """The standard toolset available to the orchestrator."""

    registry = ToolRegistry()
    registry.register(ToolSpec("task.note", "Record a plain answer", _note))
    registry.register(ToolSpec("task.verify", "Verify the final state", _verify))
    registry.register(
        ToolSpec("repo.get", "Read repository metadata", _repo_get, scope=PermissionScope.REPO_READ)
    )
    registry.register(
        ToolSpec(
            "repo.branch",
            "Create a working branch",
            _repo_branch,
            scope=PermissionScope.REPO_WRITE,
            resource_type=ResourceType.REPO,
            mutating=True,
        )
    )
    registry.register(
        ToolSpec(
            "repo.commit",
            "Commit files to a branch",
            _repo_commit,
            scope=PermissionScope.REPO_WRITE,
            resource_type=ResourceType.REPO,
            mutating=True,
        )
    )
    registry.register(
        ToolSpec(
            "repo.pr",
            "Open a pull request",
            _repo_pr,
            scope=PermissionScope.REPO_WRITE,
            resource_type=ResourceType.REPO,
            mutating=True,
        )
    )
    registry.register(
        ToolSpec(
            "render.services",
            "List tracked Render services",
            _render_services,
            scope=PermissionScope.DEPLOY_DEV,
            resource_type=ResourceType.SERVICE,
        )
    )
    registry.register(
        ToolSpec(
            "render.logs",
            "Retrieve service logs",
            _render_logs,
            scope=PermissionScope.DEPLOY_DEV,
            resource_type=ResourceType.SERVICE,
        )
    )
    registry.register(
        ToolSpec(
            "render.status",
            "Check recent deployment status",
            _render_status,
            scope=PermissionScope.DEPLOY_DEV,
            resource_type=ResourceType.SERVICE,
        )
    )
    registry.register(
        ToolSpec(
            "render.deploy",
            "Trigger a deployment",
            _render_deploy,
            scope=PermissionScope.DEPLOY_DEV,
            resource_type=ResourceType.SERVICE,
            mutating=True,
            scope_resolver=_resolve_deploy_scope,
        )
    )
    registry.register(
        ToolSpec(
            "render.restart",
            "Restart a designated service (routine operation)",
            _render_restart,
            scope=PermissionScope.DEPLOY_DEV,
            resource_type=ResourceType.SERVICE,
            mutating=True,
        )
    )
    registry.register(
        ToolSpec(
            "actions.dispatch",
            "Run an authorized GitHub Actions workflow",
            _actions_dispatch,
            scope=PermissionScope.REPO_WRITE,
            resource_type=ResourceType.REPO,
            mutating=True,
        )
    )
    registry.register(
        ToolSpec(
            "vm.inspect",
            "Inspect registered VM targets",
            _vm_inspect,
            scope=PermissionScope.VM_INSPECT,
            resource_type=ResourceType.VM,
        )
    )
    registry.register(
        ToolSpec(
            "vm.logs",
            "Read logs from a VM worker job",
            _vm_logs,
            scope=PermissionScope.VM_INSPECT,
            resource_type=ResourceType.VM,
        )
    )
    registry.register(
        ToolSpec(
            "worker.run",
            "Run a command on the VM worker",
            _worker_run,
            scope=PermissionScope.VM_EXEC,
            resource_type=ResourceType.VM,
            mutating=True,
        )
    )
    registry.register(
        ToolSpec(
            "surge.publish",
            "Publish a static site to Surge",
            _surge_publish,
            scope=PermissionScope.DEPLOY_DEV,
            resource_type=ResourceType.DOMAIN,
            mutating=True,
            destructive=True,
        )
    )
    return registry
