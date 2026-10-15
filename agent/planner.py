"""Structured task planning.

Planning is **rule-based and deterministic**. This matters for security: the plan
is produced from the request text and configuration, never from model output, and
every step carries the permission it requires. An LLM may later *propose* a plan,
but it never decides authorization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent.models import PlannedStep, TaskKind, TaskPlan
from permissions.models import PermissionScope, ResourceType

_REPO_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")
_BRANCH_RE = re.compile(r"\bbranch[:\s]+([A-Za-z0-9._/-]+)", re.IGNORECASE)
_PATH_RE = re.compile(r"\b([\w./-]+\.(?:py|js|ts|tsx|jsx|java|kt|go|rs|c|cpp|h|cs|rb|php|json|ya?ml|toml|md|sh|sql|txt))\b")


@dataclass
class PlanContext:
    """Facts about the request used to shape the plan."""

    repo: str | None = None
    branch: str | None = None
    production: bool = False


def extract_repo(prompt: str) -> str | None:
    match = _REPO_RE.search(prompt or "")
    if not match:
        return None
    candidate = match.group(1)
    # Avoid matching ordinary paths like "src/main".
    if candidate.split("/")[0] in {"src", "lib", "app", "tests", "test", "docs"}:
        return None
    return candidate


def extract_paths(prompt: str) -> list[str]:
    return sorted(set(_PATH_RE.findall(prompt or "")))


def classify(prompt: str) -> TaskKind:
    """Classify a request. Order matters: more specific intents win first."""

    lowered = (prompt or "").lower()
    # Static publishing is more specific than the generic "deploy/publish" rule.
    if any(word in lowered for word in ("surge", "static site", "static website")):
        return TaskKind.STATIC_PUBLISH
    # A request about a *failure* is diagnosis, even if it mentions deploying.
    if any(
        word in lowered
        for word in ("why", "debug", "diagnose", "failing", "failed", "error", "logs", "stack trace")
    ):
        return TaskKind.DIAGNOSE
    if any(word in lowered for word in ("pull request", "open a pr", "raise a pr", "/pr")):
        return TaskKind.PULL_REQUEST
    if any(word in lowered for word in ("deploy", "ship it", "publish", "release to")):
        if "prod" in lowered or "production" in lowered:
            return TaskKind.DEPLOY_PROD
        return TaskKind.DEPLOY_DEV
    if any(
        word in lowered
        for word in ("fix", "add", "implement", "refactor", "update", "change", "patch", "rename")
    ):
        return TaskKind.REPO_CHANGE
    if any(word in lowered for word in ("inspect", "read", "review", "explain", "summarise", "summarize", "how does")):
        return TaskKind.REPO_INSPECT
    if any(word in lowered for word in ("run ", "execute", "test in vm", "build in vm")):
        return TaskKind.VM_RUN
    return TaskKind.ANSWER


def build_plan(prompt: str, *, context: PlanContext | None = None) -> TaskPlan:
    """Produce a structured plan with an explicit permission per step."""

    context = context or PlanContext()
    repo = context.repo or extract_repo(prompt)
    kind = classify(prompt)
    steps: list[PlannedStep] = []

    repo_resource = repo or "*"

    if kind in {
        TaskKind.REPO_INSPECT,
        TaskKind.REPO_CHANGE,
        TaskKind.PULL_REQUEST,
        TaskKind.DIAGNOSE,
    }:
        if repo:
            steps.append(
                PlannedStep(
                    name="Read repository metadata",
                    tool="repo.get",
                    args={"repo": repo},
                    scope=PermissionScope.REPO_READ,
                    resource_type=ResourceType.REPO,
                    resource_id=repo_resource,
                )
            )

    if kind is TaskKind.DIAGNOSE:
        steps.append(
            PlannedStep(
                name="Retrieve deployment logs",
                tool="render.logs",
                args={"service": repo or "*"},
                scope=PermissionScope.DEPLOY_DEV,
                resource_type=ResourceType.SERVICE,
                resource_id=repo_resource,
                optional=True,
            )
        )

    if kind in {TaskKind.REPO_CHANGE, TaskKind.PULL_REQUEST}:
        steps.append(
            PlannedStep(
                name="Create working branch",
                tool="repo.branch",
                args={"repo": repo or "", "branch": context.branch or "agent/change"},
                scope=PermissionScope.REPO_WRITE,
                resource_type=ResourceType.REPO,
                resource_id=repo_resource,
            )
        )
        steps.append(
            PlannedStep(
                name="Apply code changes",
                tool="repo.commit",
                args={"repo": repo or "", "paths": extract_paths(prompt)},
                scope=PermissionScope.REPO_WRITE,
                resource_type=ResourceType.REPO,
                resource_id=repo_resource,
            )
        )
        steps.append(
            PlannedStep(
                name="Run project tests",
                tool="worker.run",
                args={"repo": repo or "", "command": ["pytest", "-q"]},
                scope=PermissionScope.VM_EXEC,
                resource_type=ResourceType.VM,
                resource_id="default",
                optional=True,
            )
        )

    if kind is TaskKind.PULL_REQUEST:
        steps.append(
            PlannedStep(
                name="Open pull request",
                tool="repo.pr",
                args={"repo": repo or "", "head": context.branch or "agent/change"},
                scope=PermissionScope.REPO_WRITE,
                resource_type=ResourceType.REPO,
                resource_id=repo_resource,
            )
        )

    if kind in {TaskKind.DEPLOY_DEV, TaskKind.DEPLOY_PROD}:
        production = kind is TaskKind.DEPLOY_PROD or context.production
        steps.append(
            PlannedStep(
                name="Inspect target service",
                tool="render.services",
                args={},
                scope=PermissionScope.DEPLOY_DEV,
                resource_type=ResourceType.SERVICE,
                resource_id=repo_resource,
                optional=True,
            )
        )
        steps.append(
            PlannedStep(
                name="Deploy to production" if production else "Deploy to development",
                tool="render.deploy",
                args={"service": repo or "", "production": production},
                scope=PermissionScope.DEPLOY_PROD if production else PermissionScope.DEPLOY_DEV,
                resource_type=ResourceType.SERVICE,
                resource_id=repo_resource,
                destructive=production,
            )
        )

    if kind is TaskKind.STATIC_PUBLISH:
        steps.append(
            PlannedStep(
                name="Publish static site",
                tool="surge.publish",
                args={"domain": ""},
                scope=PermissionScope.DEPLOY_DEV,
                resource_type=ResourceType.DOMAIN,
                resource_id="*",
                destructive=True,
            )
        )

    if kind is TaskKind.VM_RUN:
        steps.append(
            PlannedStep(
                name="Run command on VM worker",
                tool="worker.run",
                args={"command": ["pytest", "-q"]},
                scope=PermissionScope.VM_EXEC,
                resource_type=ResourceType.VM,
                resource_id="default",
            )
        )

    if not steps:
        steps.append(
            PlannedStep(name="Answer the request", tool="task.note", args={"prompt": prompt})
        )

    # Every plan ends with an explicit verification step.
    steps.append(PlannedStep(name="Verify final state", tool="task.verify", args={}))

    summary = f"{kind.value}: " + " -> ".join(step.name for step in steps)
    return TaskPlan(kind=kind, summary=summary, steps=steps)
