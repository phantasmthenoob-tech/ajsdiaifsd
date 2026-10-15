"""Wire protocol for the VM execution worker.

The Discord bot never gets unrestricted root on the VM. It talks to a narrow HTTP
API that accepts a job (image + argv + optional files), runs it in an isolated
container with resource limits, and reports status, logs, cancellation, and
cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }


@dataclass
class ResourceLimits:
    timeout_seconds: int = 120
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    network: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "memory_mb": self.memory_mb,
            "cpus": self.cpus,
            "pids": self.pids,
            "network": self.network,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "ResourceLimits":
        data = data or {}
        return cls(
            timeout_seconds=int(data.get("timeout_seconds", 120)),
            memory_mb=int(data.get("memory_mb", 512)),
            cpus=float(data.get("cpus", 1.0)),
            pids=int(data.get("pids", 128)),
            network=bool(data.get("network", False)),
        )


@dataclass
class JobRequest:
    command: list[str]
    image: str = "python:3.12-slim"
    files: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def validate(self) -> Optional[str]:
        if not self.command:
            return "A `command` (non-empty argv list) is required."
        if not self.image:
            return "An `image` is required."
        if len(self.files) > 500:
            return "Too many files supplied for one job."
        total = sum(len(v) for v in self.files.values())
        if total > 5_000_000:
            return "Job files exceed the size limit."
        # Never forward a caller-supplied env that looks like a credential host path.
        for key in self.env:
            if key.upper() in {"AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DISCORD_BOT_TOKEN"}:
                return f"Refusing to pass `{key}` into a job."
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "image": self.image,
            "files": self.files,
            "env": self.env,
            "limits": self.limits.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRequest":
        return cls(
            command=list(data.get("command") or []),
            image=str(data.get("image") or "python:3.12-slim"),
            files=dict(data.get("files") or {}),
            env=dict(data.get("env") or {}),
            limits=ResourceLimits.from_dict(data.get("limits")),
        )


@dataclass
class JobResult:
    job_id: str
    status: JobStatus
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status is JobStatus.SUCCEEDED and self.exit_code in (0, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobResult":
        return cls(
            job_id=str(data.get("job_id", "")),
            status=JobStatus(data.get("status", "queued")),
            exit_code=data.get("exit_code"),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            created_at=data.get("created_at"),
            finished_at=data.get("finished_at"),
            note=data.get("note", ""),
        )
