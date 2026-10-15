"""Isolated execution and safe static validation.

Code generation and code execution are deliberately separate features.

* :class:`StaticValidator` never runs code.  It performs syntax-only checks
  (``ast.parse`` for Python, ``json.loads`` for JSON, ``bash -n``/``node --check``
  when those tools exist).  It is always available.
* :class:`DockerSandbox` runs commands inside a locked-down container
  (``--network none``, memory/CPU/PID limits, read-only root, no host secrets).
  It is opt-in via ``ENABLE_SANDBOX_EXECUTION=true`` and ``SANDBOX_BACKEND=docker``
  and refuses to run when Docker is unavailable.

When no sandbox is available the bot performs static checks only and says so -
it never claims runtime tests were executed.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from config import SandboxConfig

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    path: str
    status: str  # 'ok' | 'error' | 'skipped'
    detail: str = ""


@dataclass
class ValidationReport:
    checks: list[CheckResult] = field(default_factory=list)
    executed: bool = False
    note: str = ""

    @property
    def ok(self) -> bool:
        return all(check.status != "error" for check in self.checks)

    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "error"]

    def summary(self) -> str:
        counts = {"ok": 0, "error": 0, "skipped": 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        parts = [f"{counts['ok']} passed", f"{counts['error']} failed", f"{counts['skipped']} skipped"]
        return ", ".join(parts)


@dataclass
class ExecutionResult:
    ran: bool
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    note: str = ""


class StaticValidator:
    """Syntax-only validation that never executes the supplied code."""

    PYTHON_EXT = {".py", ".pyi", ".pyw"}
    JSON_EXT = {".json", ".jsonc", ".json5"}
    TOML_EXT = {".toml"}
    SHELL_EXT = {".sh", ".bash"}

    def __init__(self, *, allow_subprocess: bool = True) -> None:
        self.allow_subprocess = allow_subprocess

    def validate(self, files: Mapping[str, str]) -> ValidationReport:
        report = ValidationReport(note="Static syntax validation only; code was not executed.")
        for path, content in sorted(files.items()):
            ext = os.path.splitext(path)[1].lower()
            if ext in self.PYTHON_EXT:
                report.checks.append(self._check_python(path, content))
            elif ext in self.JSON_EXT:
                report.checks.append(self._check_json(path, content))
            elif ext in self.TOML_EXT:
                report.checks.append(self._check_toml(path, content))
            elif ext in self.SHELL_EXT and self.allow_subprocess and shutil.which("bash"):
                report.checks.append(self._check_bash(path, content))
            elif ext in {".js", ".mjs", ".cjs", ".jsx"} and self.allow_subprocess and shutil.which("node"):
                report.checks.append(self._check_node(path, content))
            else:
                report.checks.append(CheckResult(path, "skipped", "no validator for this type"))
        return report

    @staticmethod
    def _check_python(path: str, content: str) -> CheckResult:
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            return CheckResult(path, "error", f"Python syntax error on line {exc.lineno}: {exc.msg}")
        return CheckResult(path, "ok")

    @staticmethod
    def _check_json(path: str, content: str) -> CheckResult:
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            return CheckResult(path, "error", f"JSON error on line {exc.lineno}: {exc.msg}")
        return CheckResult(path, "ok")

    @staticmethod
    def _check_toml(path: str, content: str) -> CheckResult:
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError:  # pragma: no cover - older interpreters
            return CheckResult(path, "skipped", "tomllib unavailable")
        try:
            tomllib.loads(content)
        except Exception as exc:  # tomllib raises TOMLDecodeError (subclass of ValueError)
            return CheckResult(path, "error", f"TOML error: {exc}")
        return CheckResult(path, "ok")

    @staticmethod
    def _check_bash(path: str, content: str) -> CheckResult:
        # `bash -n` parses without executing.
        try:
            proc = asyncio.get_event_loop().run_until_complete(_run_process(["bash", "-n"], content))
        except Exception:  # pragma: no cover - best effort
            return CheckResult(path, "skipped", "bash -n unavailable")
        if proc.returncode != 0:
            return CheckResult(path, "error", f"shell syntax error: {proc.output.strip()[:200]}")
        return CheckResult(path, "ok")

    @staticmethod
    def _check_node(path: str, content: str) -> CheckResult:
        # `node --check` parses without executing.
        try:
            proc = asyncio.get_event_loop().run_until_complete(
                _run_process(["node", "--check", "-"], content)
            )
        except Exception:  # pragma: no cover - best effort
            return CheckResult(path, "skipped", "node --check unavailable")
        if proc.returncode != 0:
            return CheckResult(path, "error", f"JS syntax error: {proc.output.strip()[:200]}")
        return CheckResult(path, "ok")


@dataclass
class _ProcessResult:
    returncode: int
    output: str


async def _run_process(command: Sequence[str], stdin_text: str, *, timeout: float = 10.0) -> _ProcessResult:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(stdin_text.encode("utf-8")), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return _ProcessResult(-1, "timed out")
    return _ProcessResult(proc.returncode or 0, stdout.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Sandbox runners
# ---------------------------------------------------------------------------
class SandboxRunner:
    """Interface for running commands against generated/uploaded files."""

    available: bool = False

    async def run(
        self,
        files: Mapping[str, str],
        command: Sequence[str],
        *,
        timeout: Optional[float] = None,
    ) -> ExecutionResult:  # pragma: no cover - interface
        raise NotImplementedError


class NullSandbox(SandboxRunner):
    """No isolated execution available: report that nothing was run."""

    available = False

    async def run(
        self, files: Mapping[str, str], command: Sequence[str], *, timeout: Optional[float] = None
    ) -> ExecutionResult:
        return ExecutionResult(
            ran=False,
            note="No secure sandbox is configured; runtime tests were not performed. "
            "Only static syntax validation was applied.",
        )


class DockerSandbox(SandboxRunner):
    """Runs commands in a locked-down, network-less container.

    The workspace is mounted read-write at ``/work`` so a test command can run,
    but there is no network, no host environment variables are forwarded, memory
    and CPU are limited, the PID count is capped, the root filesystem is
    read-only, and the container is removed afterwards.
    """

    available = True

    def __init__(self, config: SandboxConfig, *, image: str = "python:3.12-slim") -> None:
        if not shutil.which("docker"):
            raise RuntimeError("Docker is not installed or not on PATH")
        self.config = config
        self.image = image

    async def run(
        self,
        files: Mapping[str, str],
        command: Sequence[str],
        *,
        timeout: Optional[float] = None,
    ) -> ExecutionResult:
        workspace = Path(tempfile.mkdtemp(prefix="sandbox-"))
        try:
            for rel, content in files.items():
                target = (workspace / rel).resolve()
                if workspace.resolve() not in target.parents and target != workspace.resolve():
                    return ExecutionResult(ran=False, note="Refused to write outside the sandbox.")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            docker_cmd = [
                "docker", "run", "--rm",
                "--network", "none",
                "--memory", f"{self.config.memory_mb}m",
                "--cpus", str(self.config.cpu_limit),
                "--pids-limit", "128",
                "--read-only",
                "--tmpfs", "/tmp:size=64m",
                "-v", f"{workspace}:/work:rw",
                "-w", "/work",
                "--user", "65534:65534",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                self.image,
                *command,
            ]
            limit = timeout or self.config.timeout
            try:
                result = await asyncio.wait_for(
                    _run_process(docker_cmd, "", timeout=limit), timeout=limit + 5
                )
            except asyncio.TimeoutError:
                return ExecutionResult(ran=True, timed_out=True, note="Execution timed out.")
            return ExecutionResult(
                ran=True,
                exit_code=result.returncode,
                stdout=result.output,
                note="Ran inside an isolated Docker container.",
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


def create_sandbox(config: SandboxConfig) -> SandboxRunner:
    """Return the configured sandbox, falling back to static-only mode."""

    if not config.enabled:
        return NullSandbox()
    if config.backend == "docker":
        try:
            return DockerSandbox(config)
        except RuntimeError as exc:
            logger.warning("Sandbox requested but unavailable (%s); falling back to static-only.", exc)
            return NullSandbox()
    if config.backend in {"none", ""}:
        return NullSandbox()
    logger.warning("Unknown sandbox backend %r; static-only mode.", config.backend)
    return NullSandbox()
