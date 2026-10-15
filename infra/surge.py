"""Surge.sh static-site publishing.

Only domains on an explicit allowlist may be published to. The build output and
target domain are validated before any upload. Tokens are passed through the
environment and never appear in argv, logs, or artifacts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from infra.models import InfraError

logger = logging.getLogger(__name__)

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


class SurgeError(InfraError):
    pass


@dataclass
class SurgeResult:
    domain: str
    url: str
    executed: bool
    notes: str = ""


class SurgePublisher:
    def __init__(
        self,
        *,
        dry_run: bool = True,
        allowed_domains: Sequence[str] = (),
        token: Optional[str] = None,
        binary: str = "surge",
    ) -> None:
        self.dry_run = dry_run
        self.allowed_domains = tuple(d.strip().lower() for d in allowed_domains if d.strip())
        self.token = token
        self.binary = binary

    # -- validation ---------------------------------------------------------
    def check_domain(self, domain: str) -> str:
        candidate = (domain or "").strip().lower()
        if not _DOMAIN_RE.match(candidate):
            raise SurgeError(f"`{domain}` is not a valid domain name.")
        if not self.allowed_domains:
            raise SurgeError(
                "No Surge domains are authorized. Add them to SURGE_ALLOWED_DOMAINS."
            )
        for allowed in self.allowed_domains:
            if allowed.startswith("*."):
                if candidate.endswith(allowed[1:]) and candidate != allowed[2:]:
                    return candidate
            elif candidate == allowed:
                return candidate
        raise SurgeError(
            f"`{candidate}` is not in the authorized Surge domain list."
        )

    def _validate_build(self, files: Mapping[str, str]) -> None:
        if not files:
            raise SurgeError("There is no build output to publish.")
        for path in files:
            if path.startswith("/") or ".." in path:
                raise SurgeError(f"Refusing to publish unsafe path `{path}`.")
        if "index.html" not in files and not any(p.endswith("index.html") for p in files):
            raise SurgeError("The build output has no `index.html`; refusing to publish.")

    # -- publish ------------------------------------------------------------
    async def publish(self, *, domain: str, files: Mapping[str, str]) -> SurgeResult:
        target = self.check_domain(domain)
        self._validate_build(files)

        if self.dry_run or shutil.which(self.binary) is None or not self.token:
            reason = "dry run" if self.dry_run else "surge CLI or token unavailable"
            return SurgeResult(
                domain=target,
                url=f"https://{target}",
                executed=False,
                notes=f"Not published ({reason}); validated {len(files)} file(s).",
            )

        workdir = Path(tempfile.mkdtemp(prefix="surge-"))
        try:
            from files.validation import safe_project_path

            for raw_path, content in files.items():
                rel = safe_project_path(raw_path)
                target_path = (workdir / rel).resolve()
                if workdir.resolve() not in target_path.parents and target_path != workdir.resolve():
                    raise SurgeError("Refusing to write outside the build directory.")
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(content, encoding="utf-8")

            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", "/tmp"),
                "SURGE_TOKEN": self.token,
            }
            process = await asyncio.create_subprocess_exec(
                self.binary, str(workdir), target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=300)
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.wait()
                raise SurgeError("The Surge upload timed out.") from exc

            if process.returncode != 0:
                # Never surface raw CLI output that might echo the token.
                raise SurgeError("Surge rejected the deployment (non-zero exit).")
            return SurgeResult(
                domain=target,
                url=f"https://{target}",
                executed=True,
                notes=f"Published {len(files)} file(s).",
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def build_surge(config) -> SurgePublisher:
    return SurgePublisher(
        dry_run=config.agent.dry_run,
        allowed_domains=config.agent.surge_allowed_domains,
        token=config.agent.surge_token,
    )
