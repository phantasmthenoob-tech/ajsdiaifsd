"""Fly.io Machines provider adapter.

Fly Machines are real, fast-launching microVMs controlled over a REST API at
``https://api.machines.dev/v1``. The lifecycle is ``create / start / stop / delete``.

Note: Fly has **no free monthly allowance** (new organizations get a short
trial only), so this adapter is only meaningful with a paid account. It stays
disabled unless ``FLY_API_TOKEN`` and ``FLY_APP_NAME`` are configured.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp

from infra.models import CreateSpec, DeployResult, ProviderError, Target
from infra.provider import InfraProvider, safe_json

logger = logging.getLogger(__name__)

BASE_URL = "https://api.machines.dev/v1"


class FlyProvider(InfraProvider):
    kind = "fly"

    def __init__(
        self,
        api_token: str,
        *,
        app: str,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_token:
            raise ProviderError("FLY_API_TOKEN is not configured.", code="missing_token")
        if not app:
            raise ProviderError("FLY_APP_NAME is not configured.", code="missing_app")
        self.api_token = api_token
        self.app = app
        self._session = session
        self._owns_session = session is None
        self._timeout = timeout

    # -- HTTP ---------------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
            self._owns_session = True
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, *, body: Any = None) -> Any:
        session = await self._get_session()
        try:
            async with session.request(
                method, f"{BASE_URL}{path}", headers=self._headers(), json=body
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise ProviderError(
                        f"Fly rejected the request (HTTP {response.status}).",
                        code=f"fly_{response.status}",
                    )
                return safe_json(text)
        except aiohttp.ClientError as exc:
            raise ProviderError(
                f"Could not reach the Fly Machines API ({exc.__class__.__name__}).",
                code="fly_network",
            ) from exc
        except asyncio.TimeoutError as exc:
            raise ProviderError("The Fly Machines API request timed out.", code="fly_timeout") from exc

    # -- mapping ------------------------------------------------------------
    def _to_target(self, machine: dict[str, Any], fallback_name: str) -> Target:
        config = machine.get("config") or {}
        return Target(
            provider=self.kind,
            name=machine.get("name") or fallback_name,
            external_id=str(machine.get("id") or ""),
            url=None,
            status=str(machine.get("state") or "unknown"),
            raw=machine,
        )

    # -- interface ----------------------------------------------------------
    async def create(self, *, name: str, spec: CreateSpec) -> Target:
        guest = {"cpu_kind": "shared", "cpus": 1, "memory_mb": 256}
        config: dict[str, Any] = {
            "image": spec.image or "flyio/hellofly:latest",
            "auto_destroy": True,
            "guest": guest,
        }
        if spec.env:
            config["env"] = dict(spec.env)
        body: dict[str, Any] = {"name": name, "config": config}
        if spec.region:
            body["region"] = spec.region
        data = await self._request("POST", f"/apps/{self.app}/machines", body=body)
        machine = data if isinstance(data, dict) else {}
        return self._to_target(machine, name)

    async def get(self, external_id: str) -> Optional[Target]:
        data = await self._request("GET", f"/apps/{self.app}/machines/{external_id}")
        machine = data if isinstance(data, dict) else {}
        return self._to_target(machine, external_id) if machine else None

    async def list(self) -> list[Target]:
        data = await self._request("GET", f"/apps/{self.app}/machines")
        machines = data if isinstance(data, list) else []
        return [self._to_target(m, m.get("name", "")) for m in machines if isinstance(m, dict)]

    async def destroy(self, external_id: str) -> None:
        await self._request("DELETE", f"/apps/{self.app}/machines/{external_id}?force=true")

    async def deploy(
        self, *, external_id: str, image: Optional[str] = None, ref: Optional[str] = None
    ) -> DeployResult:
        if image:
            # Update the machine's image, then (re)start it.
            await self._request(
                "POST",
                f"/apps/{self.app}/machines/{external_id}",
                body={"config": {"image": image}},
            )
        await self._request("POST", f"/apps/{self.app}/machines/{external_id}/start", body=None)
        return DeployResult(
            ok=True,
            provider=self.kind,
            target=external_id,
            notes="Machine updated and started on Fly.",
        )

    async def restart(self, external_id: str) -> None:
        await self._request(
            "POST", f"/apps/{self.app}/machines/{external_id}/restart", body=None
        )

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
