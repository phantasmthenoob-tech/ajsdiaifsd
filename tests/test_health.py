"""Health endpoint tests (used by uptime monitors to keep a free service awake)."""

from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from health import build_health_app


async def test_health_endpoints_return_ok():
    client = TestClient(TestServer(build_health_app()))
    await client.start_server()
    try:
        for path in ("/health", "/healthz"):
            response = await client.get(path)
            assert response.status == 200
            payload = await response.json()
            assert payload["status"] == "ok"

        index = await client.get("/")
        assert index.status == 200
        assert "Discord AI Chatbot" in await index.text()
    finally:
        await client.close()


async def test_health_does_not_leak_state():
    client = TestClient(TestServer(build_health_app(status={"status": "ok"})))
    await client.start_server()
    try:
        response = await client.get("/health")
        payload = await response.json()
        # Only liveness information is exposed.
        assert set(payload) == {"status"}
    finally:
        await client.close()
