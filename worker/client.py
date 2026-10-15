"""Client for the VM execution worker API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp

from worker.protocol import JobRequest, JobResult, JobStatus, ResourceLimits

logger = logging.getLogger(__name__)


class WorkerError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class WorkerClient:
    """Talks only to the narrow worker API - never to the VM shell directly."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 120.0,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        if not base_url:
            raise WorkerError("WORKER_URL is not configured.")
        if not token:
            raise WorkerError("WORKER_TOKEN is not configured.")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
            self._owns_session = True
        return self._session

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, *, body: Any = None) -> Any:
        session = await self._get_session()
        try:
            async with session.request(
                method, f"{self.base_url}{path}", headers=self._headers(), json=body
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise WorkerError(
                        f"The worker rejected the request (HTTP {response.status})."
                    )
                if not text:
                    return None
                import json

                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return None
        except aiohttp.ClientError as exc:
            raise WorkerError(
                f"Could not reach the VM worker ({exc.__class__.__name__})."
            ) from exc

    async def submit(self, request: JobRequest) -> str:
        problem = request.validate()
        if problem:
            raise WorkerError(problem)
        data = await self._request("POST", "/jobs", body=request.to_dict())
        if not isinstance(data, dict) or not data.get("job_id"):
            raise WorkerError("The worker did not return a job id.")
        return str(data["job_id"])

    async def get(self, job_id: str) -> JobResult:
        data = await self._request("GET", f"/jobs/{job_id}") or {}
        return JobResult.from_dict(data)

    async def logs(self, job_id: str) -> tuple[str, str]:
        data = await self._request("GET", f"/jobs/{job_id}/logs") or {}
        return str(data.get("stdout", "")), str(data.get("stderr", ""))

    async def cancel(self, job_id: str) -> bool:
        data = await self._request("POST", f"/jobs/{job_id}/cancel") or {}
        return bool(data.get("cancelled"))

    async def run_command(
        self,
        command: list[str],
        *,
        files: Optional[dict[str, str]] = None,
        image: str = "python:3.12-slim",
        limits: Optional[ResourceLimits] = None,
        poll_interval: float = 1.0,
    ) -> JobResult:
        """Submit a job and wait for it to finish (bounded by the job timeout)."""

        request = JobRequest(
            command=command,
            image=image,
            files=files or {},
            limits=limits or ResourceLimits(timeout_seconds=int(self.timeout)),
        )
        job_id = await self.submit(request)
        deadline = asyncio.get_event_loop().time() + request.limits.timeout_seconds + 30
        while True:
            result = await self.get(job_id)
            if result.status.terminal:
                return result
            if asyncio.get_event_loop().time() > deadline:
                await self.cancel(job_id)
                return JobResult(job_id=job_id, status=JobStatus.TIMED_OUT, note="Client timed out.")
            await asyncio.sleep(poll_interval)

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
