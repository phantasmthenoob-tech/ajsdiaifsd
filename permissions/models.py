"""Permission model: explicit, owner-granted scopes per resource.

Permissions are read from the database only. They are never inferred from a
model's plan, from stored context, or from the text of a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PermissionScope(str, Enum):
    """Separate, individually grantable permission scopes."""

    AI_CONVERSATION = "ai_conversation"
    REPO_READ = "repo_read"
    REPO_WRITE = "repo_write"
    VM_INSPECT = "vm_inspect"
    VM_EXEC = "vm_exec"
    DEPLOY_DEV = "deploy_dev"
    DEPLOY_PROD = "deploy_prod"
    INFRA_ADMIN = "infra_admin"
    CONTEXT_ADMIN = "context_admin"

    @classmethod
    def parse(cls, value: str) -> "PermissionScope":
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"Unknown permission scope: {value!r}") from exc


class ResourceType(str, Enum):
    GLOBAL = "global"
    REPO = "repo"
    VM = "vm"
    SERVICE = "service"
    DOMAIN = "domain"


@dataclass
class Grant:
    principal_id: int
    scope: PermissionScope
    resource_type: ResourceType
    resource_id: str
    created_by: int
    auto_prod: bool = False
    id: Optional[int] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None

    def matches(self, *, scope: PermissionScope, resource_type: ResourceType, resource_id: str) -> bool:
        if self.scope is not scope or self.resource_type is not resource_type:
            return False
        return self.resource_id in {"*", resource_id}


@dataclass
class Decision:
    allowed: bool
    reason: str
    grant_id: Optional[int] = None

    def __bool__(self) -> bool:  # allow ``if decision:``
        return self.allowed
