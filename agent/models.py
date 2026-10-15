"""Models for autonomous task execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from permissions.models import PermissionScope, ResourceType


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
        }


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class TaskKind(str, Enum):
    ANSWER = "answer"
    REPO_INSPECT = "repo_inspect"
    REPO_CHANGE = "repo_change"
    PULL_REQUEST = "pull_request"
    DEPLOY_DEV = "deploy_dev"
    DEPLOY_PROD = "deploy_prod"
    VM_RUN = "vm_run"
    STATIC_PUBLISH = "static_publish"
    DIAGNOSE = "diagnose"


@dataclass
class ToolResult:
    """Structured outcome of a tool call. Success is never inferred from model text."""

    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    retryable: bool = False
    verified: bool = False

    @classmethod
    def success(cls, summary: str, **data: Any) -> "ToolResult":
        return cls(ok=True, summary=summary, data=data, verified=True)

    @classmethod
    def failure(cls, error: str, *, retryable: bool = False) -> "ToolResult":
        return cls(ok=False, summary=f"Failed: {error}", error=error, retryable=retryable)


@dataclass
class PlannedStep:
    """One planned action, with the permission it requires."""

    name: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    scope: Optional[PermissionScope] = None
    resource_type: ResourceType = ResourceType.GLOBAL
    resource_id: str = "*"
    destructive: bool = False
    optional: bool = False


@dataclass
class TaskPlan:
    kind: TaskKind
    summary: str
    steps: list[PlannedStep] = field(default_factory=list)


@dataclass
class TaskStep:
    task_id: int
    step_index: int
    name: str
    tool: Optional[str] = None
    args: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    result: Optional[str] = None
    error: Optional[str] = None
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class Task:
    requester_id: int
    prompt: str
    kind: TaskKind
    status: TaskStatus = TaskStatus.PENDING
    guild_id: Optional[int] = None
    channel_id: Optional[int] = None
    plan_summary: Optional[str] = None
    tool_calls: int = 0
    max_tool_calls: int = 40
    attempts: int = 0
    max_attempts: int = 3
    deadline: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    approval_id: Optional[int] = None
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    steps: list[TaskStep] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status.terminal
