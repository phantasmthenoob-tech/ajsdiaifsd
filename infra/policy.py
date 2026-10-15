"""Local guardrails that run *before* any provider call.

Provider billing alerts arrive after spend has already happened, so the only
reliable cost control is refusing the request locally.  This module enforces
name validity, per-scope target quotas, destructive-action confirmation, and a
cap on concurrent deploys.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from infra.models import PolicyError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PolicyLimits:
    max_targets: int = 5
    max_concurrent_deploys: int = 2
    name_pattern: str = r"^[a-z0-9][a-z0-9-]{1,40}$"


class PolicyGuard:
    """Enforces quotas and confirmations. Raises :class:`PolicyError` on refusal."""

    def __init__(self, limits: PolicyLimits) -> None:
        self.limits = limits
        self._name_re = re.compile(limits.name_pattern)
        self._inflight = 0

    # -- validation ---------------------------------------------------------
    def validate_name(self, name: str) -> str:
        candidate = (name or "").strip().lower()
        if not self._name_re.match(candidate):
            raise PolicyError(
                "Target names must be 2-41 characters, lowercase letters, digits or "
                "hyphens, and start with a letter or digit.",
                code="bad_name",
            )
        return candidate

    def check_create(self, *, name: str, existing_count: int) -> str:
        validated = self.validate_name(name)
        if existing_count >= self.limits.max_targets:
            raise PolicyError(
                f"Target quota reached ({self.limits.max_targets}). Destroy a target "
                "before creating another.",
                code="quota",
            )
        return validated

    def check_destroy(self, *, name: str, confirm: bool) -> str:
        validated = self.validate_name(name)
        if not confirm:
            raise PolicyError(
                f"Destroying `{validated}` is irreversible. Re-run with `confirm:true`.",
                code="needs_confirmation",
            )
        return validated

    # -- concurrency --------------------------------------------------------
    @asynccontextmanager
    async def deploy_slot(self) -> AsyncIterator[None]:
        if self._inflight >= self.limits.max_concurrent_deploys:
            raise PolicyError(
                "Too many deploys are already running. Try again shortly.",
                code="busy",
            )
        self._inflight += 1
        try:
            yield
        finally:
            self._inflight -= 1
