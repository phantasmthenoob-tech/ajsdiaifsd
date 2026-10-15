"""Render provider adapter.

Render exposes managed *services* (web / private / worker / cron / static) and
datastores - not raw VMs - over a REST API at ``https://api.render.com/v1``.

Important platform facts this adapter is built around:

* Source-based services pull from a **connected Git repo/branch**; there is no
  "upload source files" endpoint. Image-based services pull an image from a
  registry. Either way a deploy must be triggered separately.
* ``PATCH``ing a service does **not** deploy it.
* ``POST /services`` is rate-limited to ~20/hour, which is why the service layer
  enforces a target quota.

Payload shapes follow the documented API but are marked best-effort; verify
against the live OpenAPI spec when connecting a real account.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp

from infra.models import CreateSpec, DeployResult, ProviderError, Target
from infra.provider import InfraProvider, safe_json

logger = logging.getLogger(__name__)

BASE_URL = "https://api.render.com/v1"


class RenderProvider(InfraProvider):
    kind = "render"

    def __init__(
        self,
        api_key: str,
        *,
        owner_id: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ProviderError("RENDER_API_KEY is not configured.", code="missing_token")
        self.api_key = api_key
        self.owner_id = owner_id
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
            "Authorization": f"Bearer {self.api_key}",
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
                        f"Render rejected the request (HTTP {response.status}).",
                        code=f"render_{response.status}",
                    )
                return safe_json(text)
        except aiohttp.ClientError as exc:
            raise ProviderError(
                f"Could not reach the Render API ({exc.__class__.__name__}).", code="render_network"
            ) from exc
        except asyncio.TimeoutError as exc:
            raise ProviderError("The Render API request timed out.", code="render_timeout") from exc

    # -- mapping ------------------------------------------------------------
    def _to_target(self, service: dict[str, Any], fallback_name: str) -> Target:
        details = service.get("serviceDetails") or {}
        suspended = service.get("suspended")
        status = "suspended" if suspended is True else (service.get("status") or "running")
        return Target(
            provider=self.kind,
            name=service.get("name") or fallback_name,
            external_id=str(service.get("id") or ""),
            url=details.get("url") or service.get("url"),
            status=str(status),
            raw=service,
        )

    # -- interface ----------------------------------------------------------
    async def create(self, *, name: str, spec: CreateSpec) -> Target:
        body: dict[str, Any] = {
            "type": spec.service_type,
            "name": name,
            "ownerId": self.owner_id,
        }
        if spec.repo:
            body["repo"] = spec.repo
            body["branch"] = spec.branch or "main"
            body["autoDeploy"] = "yes"
        if spec.image:
            body["image"] = {"imagePath": spec.image}
        details: dict[str, Any] = {}
        if spec.plan:
            details["plan"] = spec.plan
        if spec.runtime:
            details["env"] = spec.runtime
        if spec.region:
            details["region"] = spec.region
        if details:
            body["serviceDetails"] = details
        if spec.env:
            body["envVars"] = [{"key": k, "value": v} for k, v in spec.env.items()]

        body = {k: v for k, v in body.items() if v is not None}
        data = await self._request("POST", "/services", body=body)
        service = (data or {}).get("service", data) or {}
        return self._to_target(service, name)

    async def get(self, external_id: str) -> Optional[Target]:
        data = await self._request("GET", f"/services/{external_id}")
        service = (data or {}).get("service", data) or {}
        return self._to_target(service, external_id) if service else None

    async def list(self) -> list[Target]:
        data = await self._request("GET", "/services?limit=100")
        entries = data if isinstance(data, list) else []
        targets: list[Target] = []
        for entry in entries:
            service = entry.get("service", entry) if isinstance(entry, dict) else {}
            if service:
                targets.append(self._to_target(service, service.get("name", "")))
        return targets

    async def destroy(self, external_id: str) -> None:
        await self._request("DELETE", f"/services/{external_id}")

    async def deploy(
        self, *, external_id: str, image: Optional[str] = None, ref: Optional[str] = None
    ) -> DeployResult:
        body: dict[str, Any] = {}
        if image:
            body["imageUrl"] = image
        elif ref:
            body["commitId"] = ref
        data = await self._request("POST", f"/services/{external_id}/deploys", body=body or None)
        deploy_id = None
        if isinstance(data, dict):
            deploy_id = data.get("id") or (data.get("deploy") or {}).get("id")
        return DeployResult(
            ok=True,
            provider=self.kind,
            target=external_id,
            deploy_id=str(deploy_id) if deploy_id else None,
            notes="Deploy triggered on Render (build may take a few minutes).",
        )

    async def restart(self, external_id: str) -> None:
        # Render exposes a dedicated restart operation for a service.
        await self._request("POST", f"/services/{external_id}/restart", body=None)

    async def fetch_logs(self, external_id: str, *, limit: int = 100) -> list[str]:
        """Fetch recent logs via Render's workspace-wide ``/logs`` endpoint.

        Render paginates logs by ``startTime``/``endTime`` and expects the
        resource id as a query parameter. The response schema is parsed
        defensively: an unrecognised shape is reported as an error rather than
        being presented as if it were real log output.
        """

        query = f"resource={external_id}&limit={max(1, min(limit, 500))}&direction=backward"
        if self.owner_id:
            query += f"&ownerId={self.owner_id}"
        data = await self._request("GET", f"/logs?{query}")
        entries: Any = None
        if isinstance(data, dict):
            entries = data.get("logs") or data.get("entries") or data.get("logEntries")
        elif isinstance(data, list):
            entries = data
        if not isinstance(entries, list):
            raise ProviderError(
                "Render returned a log shape this client does not recognise.",
                code="render_logs_shape",
            )
        lines: list[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                message = entry.get("message") or entry.get("text") or entry.get("msg")
                if message is None:
                    labels = entry.get("labels") or entry.get("metadata") or {}
                    message = labels.get("message") if isinstance(labels, dict) else None
                lines.append(str(message if message is not None else entry))
            else:
                lines.append(str(entry))
        return lines

    async def list_deploys(self, external_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """List recent deploys for a service, newest first."""

        data = await self._request(
            "GET", f"/services/{external_id}/deploys?limit={max(1, min(limit, 100))}"
        )
        entries = data if isinstance(data, list) else []
        deploys: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            deploy = entry.get("deploy", entry)
            if isinstance(deploy, dict):
                deploys.append(deploy)
        return deploys

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
