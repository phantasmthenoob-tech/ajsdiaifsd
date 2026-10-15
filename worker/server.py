"""VM worker HTTP server.

Runs on the dedicated Linux VM. It exposes a *narrow* API (submit / status /
logs / cancel) and executes jobs inside isolated containers with resource limits
and **no host credentials or secrets**. It must not be exposed publicly without
strong authentication (a shared bearer token) plus network restrictions.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from utils import utcnow_iso
from worker.protocol import JobRequest, JobResult, JobStatus

logger = logging.getLogger(__name__)

Runner = Callable[[JobRequest, str, str], Awaitable[tuple[Optional[int], str, str, bool]]]


@dataclass
class JobRecord:
    job_id: str
    request: JobRequest
    status: JobStatus = JobStatus.QUEUED
    result: Optional[JobResult] = None
    created_at: str = field(default_factory=utcnow_iso)
    finished_at: Optional[str] = None
    container_name: Optional[str] = None
    task: Optional[asyncio.Task] = None

    def to_result(self) -> JobResult:
        if self.result is not None:
            return self.result
        return JobResult(job_id=self.job_id, status=self.status, created_at=self.created_at)


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------
async def docker_runner(job: JobRequest, container_name: str, workdir: str) -> tuple[Optional[int], str, str, bool]:
    """Run the job in a locked-down container. Never shares host secrets."""

    if shutil.which("docker") is None:
        return None, "", "docker is not installed on the worker", False

    limits = job.limits
    command = [
        "docker", "run", "--rm",
        "--name", container_name,
        # No network by default; no host environment is forwarded.
        "--network", "bridge" if limits.network else "none",
        "--memory", f"{limits.memory_mb}m",
        "--cpus", str(limits.cpus),
        "--pids-limit", str(limits.pids),
        "--read-only",
        "--tmpfs", "/tmp:size=64m",
        "-v", f"{workdir}:/work:rw",
        "-w", "/work",
        "--user", "65534:65534",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]
    for key, value in job.env.items():
        command += ["-e", f"{key}={value}"]
    command += [job.image, *job.command]

    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        return None, "", "docker is not installed on the worker", False

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=limits.timeout_seconds
        )
        return (
            process.returncode,
            stdout.decode("utf-8", "replace")[-10000:],
            stderr.decode("utf-8", "replace")[-10000:],
            False,
        )
    except asyncio.TimeoutError:
        await kill_container(container_name)
        process.kill()
        await process.wait()
        return None, "", "job exceeded its time limit", True


async def kill_container(container_name: str) -> None:
    if shutil.which("docker") is None:
        return
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "kill", container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
    except Exception:  # pragma: no cover - best effort
        pass


class WorkerServer:
    """Owns job state and executes jobs with a concurrency cap."""

    def __init__(
        self,
        *,
        token: str,
        runner: Runner = docker_runner,
        max_concurrent: int = 2,
        history: int = 50,
    ) -> None:
        self.token = token
        self.runner = runner
        self.jobs: dict[str, JobRecord] = {}
        self._order: list[str] = []
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._history = max(1, history)

    # -- auth ---------------------------------------------------------------
    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):], self.token)

    # -- execution ----------------------------------------------------------
    async def submit(self, request: JobRequest) -> JobRecord:
        job_id = uuid.uuid4().hex[:12]
        record = JobRecord(job_id=job_id, request=request)
        self.jobs[job_id] = record
        self._order.append(job_id)
        self._trim()
        record.task = asyncio.create_task(self._execute(record))
        return record

    async def _execute(self, record: JobRecord) -> None:
        async with self._semaphore:
            if record.status is JobStatus.CANCELLED:
                return
            record.status = JobStatus.RUNNING
            workdir = Path(tempfile.mkdtemp(prefix=f"job-{record.job_id}-"))
            started = time.monotonic()
            try:
                for rel, content in record.request.files.items():
                    from files.validation import safe_project_path

                    target = (workdir / safe_project_path(rel)).resolve()
                    if workdir.resolve() not in target.parents and target != workdir.resolve():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")

                container = f"dcbot-worker-{record.job_id}"
                record.container_name = container
                exit_code, stdout, stderr, timed_out = await self.runner(
                    record.request, container, str(workdir)
                )
                duration = time.monotonic() - started

                if record.status is JobStatus.CANCELLED:
                    status = JobStatus.CANCELLED
                elif timed_out:
                    status = JobStatus.TIMED_OUT
                elif exit_code == 0:
                    status = JobStatus.SUCCEEDED
                else:
                    status = JobStatus.FAILED

                record.status = status
                record.finished_at = utcnow_iso()
                record.result = JobResult(
                    job_id=record.job_id,
                    status=status,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=round(duration, 3),
                    created_at=record.created_at,
                    finished_at=record.finished_at,
                )
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    async def cancel(self, job_id: str) -> bool:
        record = self.jobs.get(job_id)
        if record is None or record.status.terminal:
            return False
        record.status = JobStatus.CANCELLED
        if record.container_name:
            await kill_container(record.container_name)
        if record.task and not record.task.done():
            record.task.cancel()
        record.finished_at = utcnow_iso()
        record.result = JobResult(
            job_id=job_id, status=JobStatus.CANCELLED, created_at=record.created_at,
            finished_at=record.finished_at,
        )
        return True

    def _trim(self) -> None:
        while len(self._order) > self._history:
            oldest = self._order.pop(0)
            record = self.jobs.get(oldest)
            if record and record.status.terminal:
                self.jobs.pop(oldest, None)
            else:
                self._order.append(oldest)
                break

    async def shutdown(self) -> None:
        for record in self.jobs.values():
            if record.task and not record.task.done():
                record.task.cancel()
            if record.container_name:
                await kill_container(record.container_name)


# ---------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------
def build_worker_app(server: WorkerServer) -> web.Application:
    app = web.Application()

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if request.path in {"/health", "/healthz"}:
            return await handler(request)
        if not server._authorized(request):  # noqa: SLF001 - same module
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app.middlewares.append(auth_middleware)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def create_job(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        job = JobRequest.from_dict(payload or {})
        problem = job.validate()
        if problem:
            return web.json_response({"error": problem}, status=400)
        record = await server.submit(job)
        return web.json_response({"job_id": record.job_id, "status": record.status.value}, status=202)

    async def job_status(request: web.Request) -> web.Response:
        record = server.jobs.get(request.match_info["job_id"])
        if record is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(record.to_result().to_dict())

    async def job_logs(request: web.Request) -> web.Response:
        record = server.jobs.get(request.match_info["job_id"])
        if record is None:
            return web.json_response({"error": "not found"}, status=404)
        result = record.to_result()
        return web.json_response({"stdout": result.stdout, "stderr": result.stderr})

    async def job_cancel(request: web.Request) -> web.Response:
        cancelled = await server.cancel(request.match_info["job_id"])
        return web.json_response({"cancelled": cancelled})

    app.router.add_get("/health", health)
    app.router.add_get("/healthz", health)
    app.router.add_post("/jobs", create_job)
    app.router.add_get("/jobs/{job_id}", job_status)
    app.router.add_get("/jobs/{job_id}/logs", job_logs)
    app.router.add_post("/jobs/{job_id}/cancel", job_cancel)

    async def _cleanup(_app: web.Application) -> None:
        await server.shutdown()

    app.on_cleanup.append(_cleanup)
    return app


async def start_worker(
    port: int, *, token: str, host: str = "127.0.0.1", runner: Runner = docker_runner
) -> web.AppRunner:
    server = WorkerServer(token=token, runner=runner)
    runner_obj = web.AppRunner(build_worker_app(server))
    await runner_obj.setup()
    await web.TCPSite(runner_obj, host, port).start()
    logger.info("VM worker API listening on %s:%d", host, port)
    return runner_obj


if __name__ == "__main__":  # pragma: no cover - process entry point
    import os

    from aiohttp import web as _web

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    _token = os.environ.get("WORKER_TOKEN")
    if not _token:
        raise SystemExit("WORKER_TOKEN must be set before starting the worker")
    _port = int(os.environ.get("WORKER_PORT") or os.environ.get("PORT") or 8080)
    # Defaults to loopback. Only bind a wider interface behind a firewall/VPN or TLS.
    _host = os.environ.get("WORKER_HOST", "127.0.0.1")
    logger.warning(
        "Starting worker on %s:%d - never expose this publicly without network restrictions",
        _host,
        _port,
    )
    _web.run_app(build_worker_app(WorkerServer(token=_token)), host=_host, port=_port)
