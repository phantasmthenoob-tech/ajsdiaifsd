"""Infrastructure service: the orchestration layer for the control plane.

This is the only module the Discord commands talk to. It enforces policy first,
records every target in the registry, and never calls a provider directly.
"""

from __future__ import annotations

import logging
from typing import Optional

from config import Config, InfraConfig
from infra.fly import FlyProvider
from infra.models import (
    CreateSpec,
    DeployPath,
    DeployResult,
    InfraError,
    PolicyError,
    ProviderError,
    Target,
)
from infra.pipeline import (
    DockerImagePublisher,
    GitPushPublisher,
    Publisher,
    PublishRequest,
)
from infra.policy import PolicyGuard, PolicyLimits
from infra.provider import DryRunProvider, InfraProvider
from infra.registry import TargetRegistry
from infra.render import RenderProvider

logger = logging.getLogger(__name__)


class InfraService:
    """Owner-facing infrastructure operations."""

    def __init__(
        self,
        *,
        config: Config,
        providers: dict[str, InfraProvider],
        registry: TargetRegistry,
        policy: PolicyGuard,
        publishers: dict[DeployPath, Publisher],
    ) -> None:
        self.config = config
        self.infra: InfraConfig = config.infra
        self.providers = providers
        self.registry = registry
        self.policy = policy
        self.publishers = publishers

    # -- introspection ------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.infra.enabled

    def provider_kinds(self) -> list[str]:
        return sorted(self.providers)

    def is_dry_run(self) -> bool:
        """True when no real provider is wired up (nothing can spend money)."""

        return all(isinstance(p, DryRunProvider) for p in self.providers.values())

    def status_line(self) -> str:
        mode = "dry-run" if self.is_dry_run() else "LIVE"
        return f"control plane: {'enabled' if self.enabled else 'disabled'} ({mode})"

    def _provider(self, kind: str) -> InfraProvider:
        provider = self.providers.get(kind)
        if provider is None:
            raise PolicyError(f"Provider `{kind}` is not available.", code="unknown_provider")
        return provider

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise PolicyError(
                "The infrastructure control plane is disabled. Set INFRA_ENABLED=true to enable it.",
                code="disabled",
            )

    # -- operations ---------------------------------------------------------
    async def create(
        self,
        *,
        scope_key: str,
        provider: str,
        name: str,
        spec: Optional[CreateSpec] = None,
    ) -> Target:
        self._require_enabled()
        count = await self.registry.count(scope_key)
        clean_name = self.policy.check_create(name=name, existing_count=count)

        spec = spec or CreateSpec(
            repo=self.infra.git_remote,
            branch=self.infra.git_branch,
            service_type="web_service",
            runtime="docker" if self.infra.docker_registry else None,
        )
        target = await self._provider(provider).create(name=clean_name, spec=spec)
        await self.registry.upsert(scope_key, target)

        if await self.registry.get_active_name(scope_key) is None:
            await self.registry.set_active(scope_key, clean_name)
        return target

    async def list(self, *, scope_key: str) -> list[Target]:
        return await self.registry.list(scope_key)

    async def active(self, *, scope_key: str) -> Optional[Target]:
        return await self.registry.get_active(scope_key)

    async def swap(self, *, scope_key: str, name: str) -> Target:
        target = await self.registry.get(scope_key, name)
        if target is None:
            raise PolicyError(f"There is no target named `{name}` in this server.", code="missing")
        await self.registry.set_active(scope_key, target.name)
        return target

    async def destroy(self, *, scope_key: str, name: str, confirm: bool) -> str:
        clean_name = self.policy.check_destroy(name=name, confirm=confirm)
        target = await self.registry.get(scope_key, clean_name)
        if target is None:
            raise PolicyError(f"There is no target named `{clean_name}` in this server.", code="missing")

        await self._provider(target.provider).destroy(target.external_id)
        await self.registry.delete(scope_key, clean_name)
        return clean_name

    async def deploy(
        self,
        *,
        scope_key: str,
        name: Optional[str],
        path: DeployPath,
        files: dict[str, str],
        message: str = "",
    ) -> DeployResult:
        self._require_enabled()
        if not files:
            raise PolicyError("There is no code to deploy.", code="no_files")

        target = (
            await self.registry.get(scope_key, name)
            if name
            else await self.registry.get_active(scope_key)
        )
        if target is None:
            raise PolicyError(
                "No target selected. Create one with `/vm create` or pick one with `/vm swap`.",
                code="no_target",
            )

        publisher = self.publishers.get(path)
        if publisher is None:
            raise PolicyError(f"Deploy path `{path.value}` is not available.", code="bad_path")

        async with self.policy.deploy_slot():
            published = await publisher.publish(
                PublishRequest(
                    files=files,
                    message=message or f"Deploy {target.name}",
                    repo=self.infra.git_remote,
                    branch=self.infra.git_branch,
                    extra={"name": target.name},
                )
            )
            result = await self._provider(target.provider).deploy(
                external_id=target.external_id,
                image=published.reference if path is DeployPath.DOCKER else None,
                ref=published.reference if path is DeployPath.GIT else None,
            )

        notes = " · ".join(filter(None, [published.notes, result.notes]))
        result.notes = notes
        return result

    async def trigger_deploy(self, *, scope_key: str, name: Optional[str] = None) -> DeployResult:
        """Trigger a deploy of the *already-published* code for a target.

        Unlike :meth:`deploy`, this does not publish anything first - it is the
        "redeploy the current branch/image" operation.
        """

        self._require_enabled()
        target = (
            await self.registry.get(scope_key, name)
            if name
            else await self.registry.get_active(scope_key)
        )
        if target is None:
            raise PolicyError(
                "No target selected. Create one with `/vm create` or pick one with `/vm swap`.",
                code="no_target",
            )
        async with self.policy.deploy_slot():
            return await self._provider(target.provider).deploy(external_id=target.external_id)

    async def restart(self, *, scope_key: str, name: Optional[str] = None) -> Target:
        """Restart a target's process - a routine, reversible operation.

        This is a targeted restart of a single designated service, never a global
        or blanket operation.
        """

        self._require_enabled()
        target = await self._resolve_target(scope_key, name)
        await self._provider(target.provider).restart(target.external_id)
        return target

    async def logs(
        self, *, scope_key: str, name: Optional[str] = None, limit: int = 100
    ) -> tuple[Target, list[str]]:
        """Fetch recent logs for a target (honest failure if unsupported)."""

        self._require_enabled()
        target = await self._resolve_target(scope_key, name)
        try:
            lines = await self._provider(target.provider).fetch_logs(
                target.external_id, limit=limit
            )
        except ProviderError as exc:
            raise PolicyError(exc.user_message, code=exc.code or "logs_unavailable") from exc
        return target, lines

    async def deploys(
        self, *, scope_key: str, name: Optional[str] = None, limit: int = 5
    ) -> tuple[Target, list[dict]]:
        """List recent deploys for a target, for status checking."""

        self._require_enabled()
        target = await self._resolve_target(scope_key, name)
        try:
            records = await self._provider(target.provider).list_deploys(
                target.external_id, limit=limit
            )
        except ProviderError as exc:
            raise PolicyError(exc.user_message, code=exc.code or "deploys_unavailable") from exc
        return target, records

    async def _resolve_target(self, scope_key: str, name: Optional[str]) -> Target:
        target = (
            await self.registry.get(scope_key, name)
            if name
            else await self.registry.get_active(scope_key)
        )
        if target is None:
            raise PolicyError(
                "No target selected. Create one with `/vm create` or pick one with `/vm swap`.",
                code="no_target",
            )
        return target

    async def close(self) -> None:
        for provider in self.providers.values():
            try:
                await provider.close()
            except Exception:  # pragma: no cover - best effort shutdown
                logger.warning("Provider %s failed to close cleanly", provider.kind)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def _build_provider(kind: str, infra: InfraConfig) -> InfraProvider:
    """Return a real provider when fully configured, otherwise a dry run stub."""

    if not infra.enabled or infra.dry_run or not infra.provider_ready(kind):
        return DryRunProvider(kind)
    try:
        if kind == "render":
            return RenderProvider(infra.render_api_key or "", owner_id=infra.render_owner_id)
        if kind == "fly":
            return FlyProvider(infra.fly_api_token or "", app=infra.fly_app or "")
    except ProviderError as exc:
        logger.warning("Falling back to dry-run for %s: %s", kind, exc.user_message)
    return DryRunProvider(kind)


def build_infra_service(config: Config, registry: TargetRegistry) -> InfraService:
    """Construct the control plane from configuration."""

    infra = config.infra
    providers: dict[str, InfraProvider] = {
        "render": _build_provider("render", infra),
        "fly": _build_provider("fly", infra),
    }

    # Publishers only touch the outside world when the plane is enabled and not
    # in dry-run mode.
    publish_dry_run = (not infra.enabled) or infra.dry_run
    publishers: dict[DeployPath, Publisher] = {
        DeployPath.GIT: GitPushPublisher(dry_run=publish_dry_run),
        DeployPath.DOCKER: DockerImagePublisher(
            dry_run=publish_dry_run,
            registry=infra.docker_registry,
            prefix=infra.docker_image_prefix,
        ),
    }

    policy = PolicyGuard(
        PolicyLimits(
            max_targets=infra.max_targets,
            max_concurrent_deploys=infra.max_concurrent_deploys,
            name_pattern=infra.name_pattern,
        )
    )

    return InfraService(
        config=config,
        providers=providers,
        registry=registry,
        policy=policy,
        publishers=publishers,
    )


__all__ = ["InfraService", "build_infra_service", "InfraError", "PolicyError", "ProviderError"]
