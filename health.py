"""A tiny HTTP health endpoint.

Render free *web services* spin down after ~15 minutes without inbound traffic.
A Discord gateway connection is an **outbound** connection, so it does not count.
Binding this endpoint lets an uptime monitor (UptimeRobot, Better Stack, etc.)
ping the service and keep it awake.

It also lets the process satisfy Render's requirement that a web service bind to
``$PORT``. Keep the payload tiny and never expose any bot state beyond liveness.
"""

from __future__ import annotations

import logging
from typing import Optional

from aiohttp import web

logger = logging.getLogger(__name__)

INDEX_HTML = (
    "<!doctype html><meta charset=utf-8><title>Discord AI Chatbot</title>"
    "<h1>Discord AI Chatbot</h1><p>Running. This endpoint exists only for uptime "
    "monitoring.</p>"
)


def build_health_app(*, status: Optional[dict] = None) -> web.Application:
    """Build the aiohttp app served for health checks."""

    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", **(status or {})})

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/healthz", health)
    return app


async def start_health_server(port: int, *, status: Optional[dict] = None) -> web.AppRunner:
    """Start the health server on ``0.0.0.0:port`` and return the runner."""

    runner = web.AppRunner(build_health_app(status=status))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health endpoint listening on 0.0.0.0:%d", port)
    return runner
