"""Provider-independent infrastructure interface.

Every backend (Render, Fly, and the safety-focused :class:`DryRunProvider`)
implements :class:`InfraProvider`, so the service layer never knows which cloud
it is talking to.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional, Sequence

from infra.models import CreateSpec, DeployResult, ProviderError, Target

logger = logging.getLogger(__name__)


def safe_json(text: str) -> Any:
    """Parse a response body, returning ``None`` when it is not JSON."""

    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


class InfraProvider(ABC):
    """Common interface for an infrastructure backend."""

    kind: str = "unknown"

    @abstractmethod
    async def create(self, *, name: str, spec: CreateSpec) -> Target:
        """Create a new target."""

    @abstractmethod
    async def get(self, external_id: str) -> Optional[Target]:
        """Fetch one target by provider ID."""

    @abstractmethod
    async def list(self) -> list[Target]:
        """List every target this provider can see."""

    @abstractmethod
    async def destroy(self, external_id: str) -> None:
        """Delete a target."""

    @abstractmethod
    async def deploy(
        self, *, external_id: str, image: Optional[str] = None, ref: Optional[str] = None
    ) -> DeployResult:
        """Trigger a deploy using the supplied image or git ref."""

    async def restart(self, external_id: str) -> None:
        """Restart a running target (a routine, non-destructive operation).

        Providers that cannot restart raise rather than pretending to.
        """

        raise ProviderError(
            f"Provider `{self.kind}` does not support restarts.", code="restart_unsupported"
        )

    async def fetch_logs(self, external_id: str, *, limit: int = 100) -> list[str]:
        """Return recent log lines for a target, newest last.

        Providers without log access raise rather than returning invented output.
        """

        raise ProviderError(
            f"Provider `{self.kind}` does not expose logs.", code="logs_unsupported"
        )

    async def list_deploys(self, external_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Return recent deploy records for a target."""

        raise ProviderError(
            f"Provider `{self.kind}` does not expose deploys.", code="deploys_unsupported"
        )

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class DryRunProvider(InfraProvider):
    """Records what *would* happen without touching any real infrastructure.

    This is the default backend. It lets the whole control plane, policy layer
    and command surface be exercised safely and unit-tested with no network and
    no ability to spend money.
    """

    def __init__(self, kind: str = "dry-run") -> None:
        self.kind = kind
        self.targets: dict[str, Target] = {}
        self.actions: list[str] = []
        self._counter = 0

    async def create(self, *, name: str, spec: CreateSpec) -> Target:
        self._counter += 1
        external_id = f"dryrun-{self._counter}"
        target = Target(
            provider=self.kind,
            name=name,
            external_id=external_id,
            url=f"https://{name}.example.invalid",
            status="running",
            raw={"dry_run": True, "spec": spec.__dict__},
        )
        self.targets[external_id] = target
        self.actions.append(f"create {name}")
        return target

    async def get(self, external_id: str) -> Optional[Target]:
        return self.targets.get(external_id)

    async def list(self) -> list[Target]:
        return list(self.targets.values())

    async def destroy(self, external_id: str) -> None:
        self.actions.append(f"destroy {external_id}")
        self.targets.pop(external_id, None)

    async def deploy(
        self, *, external_id: str, image: Optional[str] = None, ref: Optional[str] = None
    ) -> DeployResult:
        target = self.targets.get(external_id)
        reference = image or ref or "latest"
        self.actions.append(f"deploy {external_id} ({reference})")
        return DeployResult(
            ok=True,
            provider=self.kind,
            target=target.name if target else external_id,
            deploy_id="dryrun-deploy",
            url=target.url if target else None,
            notes="Dry run: no real deploy was triggered.",
        )

    async def restart(self, external_id: str) -> None:
        self.actions.append(f"restart {external_id}")

    async def fetch_logs(self, external_id: str, *, limit: int = 100) -> list[str]:
        self.actions.append(f"logs {external_id}")
        return [f"dry-run log line for {external_id}"]

    async def list_deploys(self, external_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        self.actions.append(f"deploys {external_id}")
        return [{"id": "dryrun-deploy", "status": "live", "dry_run": True}]


def find_provider(providers: Sequence[InfraProvider], kind: str) -> Optional[InfraProvider]:
    for provider in providers:
        if provider.kind == kind:
            return provider
    return None
