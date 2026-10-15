"""Domain models and error types for the infrastructure control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ProviderKind(str, Enum):
    """Supported infrastructure backends."""

    RENDER = "render"
    FLY = "fly"
    DRY_RUN = "dry-run"


class DeployPath(str, Enum):
    """How generated code reaches a target."""

    GIT = "git"
    DOCKER = "docker"

    @classmethod
    def parse(cls, value: str) -> "DeployPath":
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"Unknown deploy path: {value!r}") from exc


@dataclass
class CreateSpec:
    """Provider-agnostic description of a target to create."""

    service_type: str = "web_service"
    plan: Optional[str] = None
    runtime: Optional[str] = None
    region: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    image: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Target:
    """A provisioned compute target (a Render service or a Fly machine)."""

    provider: str
    name: str
    external_id: str
    url: Optional[str] = None
    status: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def short(self) -> str:
        bits = [f"`{self.name}`", f"({self.provider})", self.status]
        return " ".join(bits)


@dataclass
class DeployResult:
    ok: bool
    provider: str
    target: str
    deploy_id: Optional[str] = None
    url: Optional[str] = None
    notes: str = ""


@dataclass
class PublishResult:
    """Outcome of pushing code to a git repo or an image registry."""

    path: DeployPath
    reference: str
    executed: bool
    notes: str = ""


class InfraError(Exception):
    """Base error carrying a message that is safe to show to the owner."""

    def __init__(self, user_message: str, *, code: str = "infra_error") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


class ProviderError(InfraError):
    """A cloud provider rejected a request or was unreachable."""


class PolicyError(InfraError):
    """A request was blocked by local guardrails (quotas, confirmation, names)."""


class PublishError(InfraError):
    """Code could not be published to a git remote or an image registry."""
