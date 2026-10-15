"""VM worker tests: protocol validation, client calls, and the authenticated API."""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from worker.client import WorkerClient
from worker.protocol import JobRequest, JobResult, JobStatus, ResourceLimits
from worker.server import WorkerServer, build_worker_app


def test_job_request_round_trip():
    request = JobRequest(command=["pytest", "-q"], files={"a.py": "x"}, image="python:3.12-slim")
    restored = JobRequest.from_dict(request.to_dict())
    assert restored.command == request.command
    assert restored.files == request.files
    assert restored.limits.timeout_seconds == request.limits.timeout_seconds


def test_job_request_validation():
    assert JobRequest(command=[]).validate() is not None
    assert JobRequest(command=["echo"], env={"DISCORD_BOT_TOKEN": "x"}).validate() is not None
    assert JobRequest(command=["echo"]).validate() is None


def test_job_result_ok_semantics():
    assert JobResult(job_id="1", status=JobStatus.SUCCEEDED, exit_code=0).ok is True
    assert JobResult(job_id="1", status=JobStatus.FAILED, exit_code=1).ok is False
    assert JobResult(job_id="1", status=JobStatus.RUNNING).ok is False


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[tuple] = []
        self.closed = False

    def request(self, method, url, headers=None, json=None):  # noqa: A002
        self.calls.append((method, url, headers, json))
        return self._responses.pop(0)


async def test_client_uses_bearer_token_and_parses_states():
    session = FakeSession(
        [
            FakeResponse(202, '{"job_id":"j1","status":"queued"}'),
            FakeResponse(
                200, '{"job_id":"j1","status":"succeeded","exit_code":0,"stdout":"ok"}'
            ),
        ]
    )
    client = WorkerClient("http://worker.local", "tok", session=session)

    job_id = await client.submit(JobRequest(command=["echo", "hi"]))
    assert job_id == "j1"
    result = await client.get(job_id)
    assert result.status is JobStatus.SUCCEEDED and result.ok

    _, url, headers, body = session.calls[0]
    assert url.endswith("/jobs")
    assert headers["Authorization"] == "Bearer tok"
    assert body["command"] == ["echo", "hi"]


async def test_client_rejects_invalid_job_before_sending():
    client = WorkerClient("http://worker.local", "tok", session=FakeSession([]))
    try:
        await client.submit(JobRequest(command=[]))
    except Exception as exc:  # WorkerError
        assert "command" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a validation error")


def test_client_requires_configuration():
    import pytest

    from worker.client import WorkerError

    with pytest.raises(WorkerError):
        WorkerClient("", "tok")
    with pytest.raises(WorkerError):
        WorkerClient("http://x", "")


# --------------------------------------------------------------------------- server
async def fake_runner(job, container_name, workdir):
    return 0, f"ran {job.command}", "", False


def make_server(runner=fake_runner) -> WorkerServer:
    return WorkerServer(token="sekret", runner=runner, max_concurrent=1, history=10)


async def _wait_terminal(client, job_id, headers):
    for _ in range(100):
        response = await client.get(f"/jobs/{job_id}", headers=headers)
        payload = await response.json()
        if payload["status"] in {"succeeded", "failed", "cancelled", "timed_out"}:
            return payload
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish")


async def test_server_requires_authentication():
    client = TestClient(TestServer(build_worker_app(make_server())))
    await client.start_server()
    try:
        unauthorized = await client.post("/jobs", json={"command": ["echo", "hi"]})
        assert unauthorized.status == 401

        # Health is exempt so a load balancer can probe it.
        health = await client.get("/health")
        assert health.status == 200
    finally:
        await client.close()


async def test_server_job_lifecycle():
    client = TestClient(TestServer(build_worker_app(make_server())))
    await client.start_server()
    headers = {"Authorization": "Bearer sekret"}
    try:
        created = await client.post("/jobs", json={"command": ["echo", "hi"]}, headers=headers)
        assert created.status == 202
        job_id = (await created.json())["job_id"]

        payload = await _wait_terminal(client, job_id, headers)
        assert payload["status"] == "succeeded"
        assert payload["exit_code"] == 0

        logs = await client.get(f"/jobs/{job_id}/logs", headers=headers)
        assert "ran" in (await logs.json())["stdout"]
    finally:
        await client.close()


async def test_server_rejects_invalid_job():
    client = TestClient(TestServer(build_worker_app(make_server())))
    await client.start_server()
    headers = {"Authorization": "Bearer sekret"}
    try:
        response = await client.post("/jobs", json={"command": []}, headers=headers)
        assert response.status == 400
    finally:
        await client.close()


async def test_server_cancel_is_idempotent_for_unknown_jobs():
    client = TestClient(TestServer(build_worker_app(make_server())))
    await client.start_server()
    headers = {"Authorization": "Bearer sekret"}
    try:
        response = await client.post("/jobs/nope/cancel", headers=headers)
        assert response.status == 200
        assert (await response.json())["cancelled"] is False
    finally:
        await client.close()
