"""Deployment pipeline: get generated code to where the provider can pull it.

Two strategies are supported, both dry-run safe:

* :class:`GitPushPublisher` writes files to a scratch checkout, commits, and
  pushes to the configured remote/branch. The provider then builds on its own
  (Render auto-deploy, or an explicitly triggered deploy).
* :class:`DockerImagePublisher` builds a Docker image from the generated files
  and pushes it to a registry; the provider then deploys that image.

Neither path ever embeds a credential in a file or argument: git relies on the
host's credential helper and docker on the host's registry login, exactly as the
machine is already configured. Command output is scrubbed of ``user:pass@``
URLs before being surfaced.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from infra.models import DeployPath, PublishError, PublishResult

logger = logging.getLogger(__name__)

_CREDENTIAL_RE = re.compile(r"://[^/@\s]+@")


def scrub_credentials(text: str) -> str:
    """Remove ``user:pass@`` style credentials that tools may print."""

    return _CREDENTIAL_RE.sub("://***@", text or "")


async def _run(
    command: Sequence[str], *, cwd: Optional[Path] = None, timeout: float = 300.0
) -> str:
    """Run a tool, capturing combined output. Never uses a shell."""

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise PublishError(
            f"Required tool `{command[0]}` is not installed on the host.", code="tool_missing"
        ) from exc

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise PublishError(f"`{command[0]}` timed out.", code="tool_timeout") from exc

    output = scrub_credentials(stdout.decode("utf-8", "replace"))
    if process.returncode != 0:
        raise PublishError(
            f"`{command[0]}` failed: {output.strip()[:300] or 'no output'}", code="tool_failed"
        )
    return output


@dataclass
class PublishRequest:
    files: dict[str, str]
    message: str = "Update from Discord bot"
    repo: Optional[str] = None
    branch: str = "main"
    image: Optional[str] = None
    extra: dict[str, str] = field(default_factory=dict)


class Publisher(ABC):
    """Publishes a set of generated files somewhere a provider can pull from."""

    path: DeployPath

    def __init__(self, *, dry_run: bool) -> None:
        self.dry_run = dry_run

    @abstractmethod
    async def publish(self, request: PublishRequest) -> PublishResult:  # pragma: no cover
        raise NotImplementedError


def _write_files(base: Path, files: dict[str, str]) -> None:
    from files.validation import FileValidationError, safe_project_path

    root = base.resolve()
    for raw_path, content in files.items():
        try:
            rel = safe_project_path(raw_path)
        except FileValidationError as exc:
            raise PublishError(exc.user_message, code="bad_path") from exc
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise PublishError("Refusing to write outside the workspace.", code="traversal")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


class GitPushPublisher(Publisher):
    path = DeployPath.GIT

    def __init__(self, *, dry_run: bool, binary: str = "git") -> None:
        super().__init__(dry_run=dry_run)
        self.binary = binary

    async def publish(self, request: PublishRequest) -> PublishResult:
        repo = request.repo
        if not repo:
            raise PublishError(
                "No git remote is configured. Set INFRA_GIT_REMOTE (or pass a repo).",
                code="no_remote",
            )
        if not request.files:
            raise PublishError("There is no code to publish.", code="no_files")

        reference = f"{repo}#{request.branch}"
        if self.dry_run or shutil.which(self.binary) is None:
            return PublishResult(
                path=self.path,
                reference=reference,
                executed=False,
                notes="Dry run: would commit and push the generated files.",
            )

        workdir = Path(tempfile.mkdtemp(prefix="infra-git-"))
        try:
            _write_files(workdir, request.files)
            await _run([self.binary, "init", "-q"], cwd=workdir)
            await _run([self.binary, "checkout", "-q", "-b", request.branch], cwd=workdir)
            await _run([self.binary, "config", "user.email", "bot@localhost"], cwd=workdir)
            await _run([self.binary, "config", "user.name", "Discord AI Bot"], cwd=workdir)
            await _run([self.binary, "remote", "add", "origin", repo], cwd=workdir)
            await _run([self.binary, "add", "-A"], cwd=workdir)
            await _run([self.binary, "commit", "-q", "-m", request.message], cwd=workdir)
            # Never force-push: a diverged branch fails loudly rather than
            # destroying someone's history.
            await _run(
                [self.binary, "push", "origin", f"HEAD:{request.branch}"], cwd=workdir
            )
            return PublishResult(
                path=self.path,
                reference=reference,
                executed=True,
                notes=f"Pushed {len(request.files)} file(s) to `{request.branch}`.",
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


class DockerImagePublisher(Publisher):
    path = DeployPath.DOCKER

    def __init__(
        self,
        *,
        dry_run: bool,
        registry: Optional[str] = None,
        prefix: str = "discord-bot",
        tag: str = "latest",
        binary: str = "docker",
    ) -> None:
        super().__init__(dry_run=dry_run)
        self.registry = registry
        self.prefix = prefix
        self.tag = tag
        self.binary = binary

    def image_name(self, name: str) -> str:
        image = f"{self.prefix}-{name}:{self.tag}"
        return f"{self.registry}/{image}" if self.registry else image

    async def publish(self, request: PublishRequest) -> PublishResult:
        name = request.extra.get("name", "app")
        image = request.image or self.image_name(name)

        if not any(path.endswith("Dockerfile") or path == "Dockerfile" for path in request.files):
            raise PublishError(
                "The docker deploy path needs a `Dockerfile` in the generated files.",
                code="no_dockerfile",
            )

        if self.dry_run or shutil.which(self.binary) is None:
            return PublishResult(
                path=self.path,
                reference=image,
                executed=False,
                notes="Dry run: would build and push the Docker image.",
            )

        workdir = Path(tempfile.mkdtemp(prefix="infra-docker-"))
        try:
            _write_files(workdir, request.files)
            await _run([self.binary, "build", "-t", image, str(workdir)], timeout=900.0)
            await _run([self.binary, "push", image], timeout=900.0)
            return PublishResult(
                path=self.path,
                reference=image,
                executed=True,
                notes=f"Built and pushed `{image}`.",
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
