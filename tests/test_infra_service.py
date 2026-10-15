"""InfraService tests: create/list/swap/destroy/deploy via the dry-run plane."""

from __future__ import annotations

import pytest

from config import Config
from infra.models import DeployPath, PolicyError, PublishError
from infra.registry import TargetRegistry
from infra.service import build_infra_service

SCOPE = "g1"


async def test_disabled_plane_refuses_operations(config: Config, infra_registry: TargetRegistry):
    service = build_infra_service(config, infra_registry)  # INFRA_ENABLED is absent -> disabled
    assert service.enabled is False
    with pytest.raises(PolicyError):
        await service.create(scope_key=SCOPE, provider="render", name="app")


async def test_create_registers_and_activates_first_target(infra_service, infra_registry):
    target = await infra_service.create(scope_key=SCOPE, provider="render", name="my-app")
    assert target.name == "my-app"
    assert target.provider == "render"

    stored = await infra_registry.get(SCOPE, "my-app")
    assert stored is not None and stored.external_id == target.external_id
    active = await infra_service.active(scope_key=SCOPE)
    assert active is not None and active.name == "my-app"


async def test_second_create_does_not_steal_active(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="one")
    await infra_service.create(scope_key=SCOPE, provider="fly", name="two")
    active = await infra_service.active(scope_key=SCOPE)
    assert active is not None and active.name == "one"


async def test_quota_is_enforced(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="one")
    await infra_service.create(scope_key=SCOPE, provider="render", name="two")
    with pytest.raises(PolicyError):
        await infra_service.create(scope_key=SCOPE, provider="render", name="three")


async def test_swap_changes_active_target(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="one")
    await infra_service.create(scope_key=SCOPE, provider="render", name="two")
    swapped = await infra_service.swap(scope_key=SCOPE, name="two")
    assert swapped.name == "two"
    active = await infra_service.active(scope_key=SCOPE)
    assert active is not None and active.name == "two"


async def test_swap_unknown_target_is_refused(infra_service):
    with pytest.raises(PolicyError):
        await infra_service.swap(scope_key=SCOPE, name="ghost")


async def test_deploy_uses_active_target(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="app")
    result = await infra_service.deploy(
        scope_key=SCOPE,
        name=None,
        path=DeployPath.GIT,
        files={"main.py": "print('hi')\n"},
    )
    assert result.ok is True
    assert result.target == "app"
    assert "dry run" in result.notes.lower()


async def test_deploy_without_target_is_refused(infra_service):
    with pytest.raises(PolicyError):
        await infra_service.deploy(
            scope_key=SCOPE, name=None, path=DeployPath.GIT, files={"a.py": "x"}
        )


async def test_deploy_requires_files(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="app")
    with pytest.raises(PolicyError):
        await infra_service.deploy(scope_key=SCOPE, name="app", path=DeployPath.GIT, files={})


async def test_docker_deploy_path_requires_dockerfile(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="app")
    with pytest.raises(PublishError):
        await infra_service.deploy(
            scope_key=SCOPE, name="app", path=DeployPath.DOCKER, files={"main.py": "x"}
        )


async def test_docker_deploy_with_dockerfile(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="app")
    result = await infra_service.deploy(
        scope_key=SCOPE,
        name="app",
        path=DeployPath.DOCKER,
        files={"Dockerfile": "FROM scratch\n", "main.py": "x"},
    )
    assert result.ok is True


async def test_restart_targets_the_active_service(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="app")
    target = await infra_service.restart(scope_key=SCOPE)
    assert target.name == "app"


async def test_restart_named_target(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="one")
    await infra_service.create(scope_key=SCOPE, provider="render", name="two")
    target = await infra_service.restart(scope_key=SCOPE, name="two")
    assert target.name == "two"


async def test_restart_unknown_target_is_refused(infra_service):
    with pytest.raises(PolicyError):
        await infra_service.restart(scope_key=SCOPE, name="ghost")


async def test_logs_and_deploys_use_the_active_target(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="app")
    target, lines = await infra_service.logs(scope_key=SCOPE)
    assert target.name == "app" and lines

    _, records = await infra_service.deploys(scope_key=SCOPE)
    assert records and records[0]["status"] == "live"


async def test_logs_without_target_is_refused(infra_service):
    with pytest.raises(PolicyError):
        await infra_service.logs(scope_key=SCOPE)


async def test_destroy_requires_confirmation_then_removes(infra_service):
    await infra_service.create(scope_key=SCOPE, provider="render", name="app")
    with pytest.raises(PolicyError):
        await infra_service.destroy(scope_key=SCOPE, name="app", confirm=False)

    destroyed = await infra_service.destroy(scope_key=SCOPE, name="app", confirm=True)
    assert destroyed == "app"
    assert await infra_service.list(scope_key=SCOPE) == []


async def test_plane_reports_dry_run(infra_service):
    assert infra_service.is_dry_run() is True
    assert "dry-run" in infra_service.status_line()
    assert infra_service.provider_kinds() == ["fly", "render"]


async def test_scopes_are_isolated(infra_service):
    await infra_service.create(scope_key="g1", provider="render", name="alpha")
    assert await infra_service.active(scope_key="g2") is None
    assert await infra_service.list(scope_key="g2") == []
