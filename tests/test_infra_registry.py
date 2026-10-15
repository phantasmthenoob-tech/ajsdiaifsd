"""Target registry tests: CRUD, active pointer, and scope isolation."""

from __future__ import annotations

from infra.models import Target


def target(name: str, provider: str = "render") -> Target:
    return Target(provider=provider, name=name, external_id=f"ext-{name}", url=f"https://{name}.example")


async def test_upsert_get_and_list(infra_registry):
    await infra_registry.upsert("g1", target("alpha"))
    await infra_registry.upsert("g1", target("beta"))

    fetched = await infra_registry.get("g1", "alpha")
    assert fetched is not None and fetched.external_id == "ext-alpha"
    assert [t.name for t in await infra_registry.list("g1")] == ["alpha", "beta"]
    assert await infra_registry.count("g1") == 2


async def test_upsert_updates_in_place(infra_registry):
    await infra_registry.upsert("g1", target("alpha"))
    updated = Target(provider="fly", name="alpha", external_id="new-id", status="running")
    await infra_registry.upsert("g1", updated)

    fetched = await infra_registry.get("g1", "alpha")
    assert fetched is not None
    assert fetched.external_id == "new-id"
    assert fetched.provider == "fly"
    assert await infra_registry.count("g1") == 1


async def test_scope_isolation(infra_registry):
    await infra_registry.upsert("g1", target("alpha"))
    await infra_registry.upsert("g2", target("beta"))

    assert [t.name for t in await infra_registry.list("g1")] == ["alpha"]
    assert await infra_registry.get("g1", "beta") is None
    assert await infra_registry.count("g2") == 1


async def test_active_pointer(infra_registry):
    await infra_registry.upsert("g1", target("alpha"))
    await infra_registry.upsert("g1", target("beta"))

    assert await infra_registry.get_active_name("g1") is None
    await infra_registry.set_active("g1", "beta")
    active = await infra_registry.get_active("g1")
    assert active is not None and active.name == "beta"


async def test_delete_removes_target_and_clears_active(infra_registry):
    await infra_registry.upsert("g1", target("alpha"))
    await infra_registry.set_active("g1", "alpha")

    assert await infra_registry.delete("g1", "alpha") is True
    assert await infra_registry.get("g1", "alpha") is None
    assert await infra_registry.get_active_name("g1") is None
    assert await infra_registry.delete("g1", "alpha") is False
