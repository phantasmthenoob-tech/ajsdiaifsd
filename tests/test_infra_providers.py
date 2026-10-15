"""Render and Fly adapter tests using a fake aiohttp session."""

from __future__ import annotations

import pytest

from infra.fly import FlyProvider
from infra.models import CreateSpec, ProviderError
from infra.provider import DryRunProvider
from infra.render import RenderProvider


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[tuple] = []
        self.closed = False

    def request(self, method, url, headers=None, json=None):  # noqa: A002
        self.calls.append((method, url, headers, json))
        return self._responses.pop(0)


# --------------------------------------------------------------------------- render
async def test_render_create_posts_service():
    session = FakeSession(
        [
            FakeResponse(
                201,
                '{"service":{"id":"srv-1","name":"app",'
                '"serviceDetails":{"url":"https://app.onrender.com"}}}',
            )
        ]
    )
    provider = RenderProvider("key", owner_id="own-1", session=session)
    target = await provider.create(
        name="app", spec=CreateSpec(repo="https://github.com/x/y", branch="dev")
    )
    assert target.external_id == "srv-1"
    assert target.url == "https://app.onrender.com"

    method, url, headers, body = session.calls[0]
    assert method == "POST" and url.endswith("/services")
    assert headers["Authorization"] == "Bearer key"
    assert body["name"] == "app"
    assert body["ownerId"] == "own-1"
    assert body["branch"] == "dev"


async def test_render_deploy_sends_image_url():
    session = FakeSession([FakeResponse(201, '{"id":"dep-1"}')])
    provider = RenderProvider("key", session=session)
    result = await provider.deploy(external_id="srv-1", image="registry/app:latest")
    assert result.deploy_id == "dep-1"
    _, url, _, body = session.calls[0]
    assert url.endswith("/services/srv-1/deploys")
    assert body["imageUrl"] == "registry/app:latest"


async def test_render_restart_uses_restart_endpoint():
    session = FakeSession([FakeResponse(200, "{}")])
    provider = RenderProvider("key", session=session)
    await provider.restart("srv-1")
    method, url, _, _ = session.calls[0]
    assert method == "POST" and url.endswith("/services/srv-1/restart")


async def test_render_destroy_uses_delete():
    session = FakeSession([FakeResponse(204, "")])
    provider = RenderProvider("key", session=session)
    await provider.destroy("srv-1")
    method, url, _, _ = session.calls[0]
    assert method == "DELETE" and url.endswith("/services/srv-1")


async def test_render_fetch_logs_parses_entries():
    session = FakeSession(
        [FakeResponse(200, '{"logs":[{"message":"booting"},{"message":"ready"}]}')]
    )
    provider = RenderProvider("key", owner_id="own-1", session=session)
    lines = await provider.fetch_logs("srv-1", limit=50)
    assert lines == ["booting", "ready"]
    _, url, _, _ = session.calls[0]
    assert "/logs?" in url and "resource=srv-1" in url and "ownerId=own-1" in url


async def test_render_fetch_logs_rejects_unknown_shape():
    session = FakeSession([FakeResponse(200, '{"unexpected":true}')])
    provider = RenderProvider("key", session=session)
    with pytest.raises(ProviderError):
        await provider.fetch_logs("srv-1")


async def test_render_list_deploys_returns_records():
    session = FakeSession(
        [FakeResponse(200, '[{"deploy":{"id":"dep-1","status":"live"}},{"deploy":{"id":"dep-2","status":"build_failed"}}]')]
    )
    provider = RenderProvider("key", session=session)
    deploys = await provider.list_deploys("srv-1", limit=2)
    assert [d["id"] for d in deploys] == ["dep-1", "dep-2"]
    assert session.calls[0][1].endswith("/services/srv-1/deploys?limit=2")


async def test_render_errors_map_to_provider_error():
    session = FakeSession([FakeResponse(401, "unauthorized")])
    provider = RenderProvider("key", session=session)
    with pytest.raises(ProviderError):
        await provider.list()


def test_render_requires_api_key():
    with pytest.raises(ProviderError):
        RenderProvider("")


# --------------------------------------------------------------------------- fly
async def test_fly_create_machine():
    session = FakeSession(
        [FakeResponse(201, '{"id":"m1","name":"app","state":"starting","config":{"image":"x"}}')]
    )
    provider = FlyProvider("token", app="myapp", session=session)
    target = await provider.create(name="app", spec=CreateSpec(image="x", region="iad"))
    assert target.external_id == "m1"
    assert target.status == "starting"

    _, url, headers, body = session.calls[0]
    assert url.endswith("/apps/myapp/machines")
    assert headers["Authorization"] == "Bearer token"
    assert body["config"]["image"] == "x"
    assert body["region"] == "iad"


async def test_fly_destroy_forces_removal():
    session = FakeSession([FakeResponse(200, "{}")])
    provider = FlyProvider("token", app="myapp", session=session)
    await provider.destroy("m1")
    method, url, _, _ = session.calls[0]
    assert method == "DELETE" and "force=true" in url


async def test_fly_deploy_updates_then_starts():
    session = FakeSession([FakeResponse(200, "{}"), FakeResponse(200, "{}")])
    provider = FlyProvider("token", app="myapp", session=session)
    await provider.deploy(external_id="m1", image="registry/app:1")
    assert len(session.calls) == 2
    assert session.calls[1][1].endswith("/machines/m1/start")


async def test_fly_restart_targets_the_machine():
    session = FakeSession([FakeResponse(200, "{}")])
    provider = FlyProvider("token", app="myapp", session=session)
    await provider.restart("m1")
    method, url, _, _ = session.calls[0]
    assert method == "POST" and url.endswith("/apps/myapp/machines/m1/restart")


def test_fly_requires_app():
    with pytest.raises(ProviderError):
        FlyProvider("token", app="")


# --------------------------------------------------------------------------- dry run
async def test_fly_has_no_log_endpoint():
    session = FakeSession([])
    provider = FlyProvider("token", app="myapp", session=session)
    with pytest.raises(ProviderError):
        await provider.fetch_logs("m1")
    assert session.calls == []


async def test_dry_run_provider_never_touches_network():
    provider = DryRunProvider("render")
    target = await provider.create(name="app", spec=CreateSpec(plan="starter"))
    assert (await provider.list())[0].name == "app"
    assert await provider.get(target.external_id) is not None

    result = await provider.deploy(external_id=target.external_id, image="img:1")
    assert result.ok is True and result.provider == "render"

    await provider.restart(target.external_id)
    await provider.destroy(target.external_id)
    assert await provider.list() == []
    assert len(provider.actions) == 4
    assert "restart" in provider.actions[2]
