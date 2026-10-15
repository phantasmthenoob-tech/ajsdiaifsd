"""GitHub integration: one interface, REST and dry-run implementations.

The whole application talks to :class:`GitHubIntegration`; whether the credential
behind it is a GitHub App installation token or a PAT is invisible here.
"""

from __future__ import annotations

import base64
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional, Sequence

import aiohttp

from github.auth import GitHubAuth
from github.models import (
    BranchInfo,
    FileContent,
    PullRequestInfo,
    RepoRef,
    WorkflowRun,
)

logger = logging.getLogger(__name__)

API_VERSION = "2022-11-28"


class GitHubError(Exception):
    def __init__(self, user_message: str, *, status: Optional[int] = None) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.status = status


class GitHubIntegration(ABC):
    """Read/write access to repositories, pull requests and workflows."""

    method: str = "unknown"

    @abstractmethod
    async def get_repo(self, full_name: str) -> dict[str, Any]: ...

    @abstractmethod
    async def list_branches(self, full_name: str) -> list[BranchInfo]: ...

    @abstractmethod
    async def get_branch_sha(self, full_name: str, branch: str) -> Optional[str]: ...

    @abstractmethod
    async def get_file(self, full_name: str, path: str, ref: Optional[str] = None) -> Optional[FileContent]: ...

    @abstractmethod
    async def create_branch(self, full_name: str, branch: str, from_sha: str) -> BranchInfo: ...

    @abstractmethod
    async def put_files(
        self, full_name: str, branch: str, files: dict[str, str], message: str
    ) -> str: ...

    @abstractmethod
    async def create_pull_request(
        self, full_name: str, head: str, base: str, title: str, body: str = ""
    ) -> PullRequestInfo: ...

    @abstractmethod
    async def list_pull_requests(
        self, full_name: str, state: str = "open"
    ) -> list[PullRequestInfo]: ...

    @abstractmethod
    async def list_workflow_runs(self, full_name: str) -> list[WorkflowRun]: ...

    @abstractmethod
    async def dispatch_workflow(
        self,
        full_name: str,
        workflow: str,
        ref: str,
        inputs: Optional[dict[str, Any]] = None,
    ) -> None: ...

    @abstractmethod
    async def search_code(self, query: str) -> list[dict[str, Any]]: ...

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class RestGitHubIntegration(GitHubIntegration):
    """Talks to the real GitHub REST API."""

    def __init__(
        self,
        auth: GitHubAuth,
        *,
        api_base: str = "https://api.github.com",
        session: Optional[aiohttp.ClientSession] = None,
        timeout: float = 30.0,
    ) -> None:
        self.auth = auth
        self.method = auth.method
        self.api_base = api_base.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._timeout = timeout

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
            self._owns_session = True
        return self._session

    async def _request(
        self, method: str, path: str, *, body: Any = None, allow_404: bool = False
    ) -> Any:
        session = await self._get_session()
        token = await self.auth.token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        try:
            async with session.request(method, url, headers=headers, json=body) as response:
                if response.status == 404 and allow_404:
                    return None
                text = await response.text()
                if response.status >= 400:
                    # Never echo headers or the token back to the user.
                    raise GitHubError(
                        f"GitHub API error (HTTP {response.status}) for {method} {path}.",
                        status=response.status,
                    )
                if not text:
                    return None
                import json

                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return None
        except aiohttp.ClientError as exc:
            raise GitHubError(f"Could not reach GitHub ({exc.__class__.__name__}).") from exc

    # -- reads --------------------------------------------------------------
    async def get_repo(self, full_name: str) -> dict[str, Any]:
        repo = RepoRef.parse(full_name)
        return await self._request("GET", f"/repos/{repo.full_name}") or {}

    async def list_branches(self, full_name: str) -> list[BranchInfo]:
        repo = RepoRef.parse(full_name)
        data = await self._request("GET", f"/repos/{repo.full_name}/branches?per_page=100") or []
        return [
            BranchInfo(name=item["name"], sha=(item.get("commit") or {}).get("sha", ""))
            for item in data
            if isinstance(item, dict)
        ]

    async def get_branch_sha(self, full_name: str, branch: str) -> Optional[str]:
        repo = RepoRef.parse(full_name)
        data = await self._request(
            "GET", f"/repos/{repo.full_name}/git/ref/heads/{branch}", allow_404=True
        )
        if not data:
            return None
        return (data.get("object") or {}).get("sha")

    async def get_file(self, full_name: str, path: str, ref: Optional[str] = None) -> Optional[FileContent]:
        repo = RepoRef.parse(full_name)
        suffix = f"?ref={ref}" if ref else ""
        data = await self._request(
            "GET", f"/repos/{repo.full_name}/contents/{path}{suffix}", allow_404=True
        )
        if not data or not isinstance(data, dict) or "content" not in data:
            return None
        content = base64.b64decode(data["content"]).decode("utf-8", "replace")
        return FileContent(path=path, content=content, sha=data.get("sha"))

    async def search_code(self, query: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/search/code?q={query}&per_page=30") or {}
        return list(data.get("items") or [])

    async def list_workflow_runs(self, full_name: str) -> list[WorkflowRun]:
        repo = RepoRef.parse(full_name)
        data = await self._request(
            "GET", f"/repos/{repo.full_name}/actions/runs?per_page=20"
        ) or {}
        runs = []
        for item in data.get("workflow_runs") or []:
            runs.append(
                WorkflowRun(
                    id=int(item.get("id", 0)),
                    name=str(item.get("name", "")),
                    status=str(item.get("status", "")),
                    conclusion=item.get("conclusion"),
                    url=item.get("html_url"),
                )
            )
        return runs

    async def dispatch_workflow(
        self,
        full_name: str,
        workflow: str,
        ref: str,
        inputs: Optional[dict[str, Any]] = None,
    ) -> None:
        """Trigger a ``workflow_dispatch`` run of an authorized workflow."""

        repo = RepoRef.parse(full_name)
        body: dict[str, Any] = {"ref": ref}
        if inputs:
            body["inputs"] = inputs
        await self._request(
            "POST",
            f"/repos/{repo.full_name}/actions/workflows/{workflow}/dispatches",
            body=body,
        )

    # -- writes -------------------------------------------------------------
    async def create_branch(self, full_name: str, branch: str, from_sha: str) -> BranchInfo:
        repo = RepoRef.parse(full_name)
        await self._request(
            "POST",
            f"/repos/{repo.full_name}/git/refs",
            body={"ref": f"refs/heads/{branch}", "sha": from_sha},
        )
        return BranchInfo(name=branch, sha=from_sha)

    async def put_files(
        self, full_name: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        repo = RepoRef.parse(full_name)
        last_sha = ""
        for path, content in files.items():
            existing = await self._request(
                "GET",
                f"/repos/{repo.full_name}/contents/{path}?ref={branch}",
                allow_404=True,
            )
            body: dict[str, Any] = {
                "message": message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            if isinstance(existing, dict) and existing.get("sha"):
                body["sha"] = existing["sha"]
            result = await self._request(
                "PUT", f"/repos/{repo.full_name}/contents/{path}", body=body
            )
            if isinstance(result, dict):
                last_sha = (result.get("commit") or {}).get("sha", last_sha)
        return last_sha

    async def create_pull_request(
        self, full_name: str, head: str, base: str, title: str, body: str = ""
    ) -> PullRequestInfo:
        repo = RepoRef.parse(full_name)
        data = await self._request(
            "POST",
            f"/repos/{repo.full_name}/pulls",
            body={"title": title, "head": head, "base": base, "body": body},
        ) or {}
        return PullRequestInfo(
            number=int(data.get("number", 0)),
            url=str(data.get("html_url", "")),
            title=str(data.get("title", title)),
            head=head,
            base=base,
            state=str(data.get("state", "open")),
        )

    async def list_pull_requests(
        self, full_name: str, state: str = "open"
    ) -> list[PullRequestInfo]:
        repo = RepoRef.parse(full_name)
        data = await self._request(
            "GET", f"/repos/{repo.full_name}/pulls?state={state}&per_page=50"
        ) or []
        return [
            PullRequestInfo(
                number=int(item.get("number", 0)),
                url=str(item.get("html_url", "")),
                title=str(item.get("title", "")),
                head=(item.get("head") or {}).get("ref", ""),
                base=(item.get("base") or {}).get("ref", ""),
                state=str(item.get("state", state)),
            )
            for item in data
            if isinstance(item, dict)
        ]

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None


class DryRunGitHubIntegration(GitHubIntegration):
    """Records intended GitHub actions without contacting GitHub."""

    method = "dry-run"

    def __init__(self, *, base_branch: str = "main") -> None:
        self.base_branch = base_branch
        self.calls: list[tuple[str, tuple]] = []
        self._branches: dict[str, BranchInfo] = {
            base_branch: BranchInfo(name=base_branch, sha="dryrun-base-sha")
        }

    async def get_repo(self, full_name: str) -> dict[str, Any]:
        self.calls.append(("get_repo", (full_name,)))
        return {"full_name": full_name, "default_branch": self.base_branch}

    async def list_branches(self, full_name: str) -> list[BranchInfo]:
        self.calls.append(("list_branches", (full_name,)))
        return list(self._branches.values())

    async def get_branch_sha(self, full_name: str, branch: str) -> Optional[str]:
        self.calls.append(("get_branch_sha", (full_name, branch)))
        info = self._branches.get(branch)
        return info.sha if info else None

    async def get_file(self, full_name: str, path: str, ref: Optional[str] = None) -> Optional[FileContent]:
        self.calls.append(("get_file", (full_name, path, ref)))
        return FileContent(path=path, content=f"# dry-run contents of {path}\n", sha="dryrun-file-sha")

    async def create_branch(self, full_name: str, branch: str, from_sha: str) -> BranchInfo:
        self.calls.append(("create_branch", (full_name, branch, from_sha)))
        info = BranchInfo(name=branch, sha=from_sha)
        self._branches[branch] = info
        return info

    async def put_files(
        self, full_name: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        self.calls.append(("put_files", (full_name, branch, tuple(sorted(files)), message)))
        return "dryrun-commit-sha"

    async def create_pull_request(
        self, full_name: str, head: str, base: str, title: str, body: str = ""
    ) -> PullRequestInfo:
        self.calls.append(("create_pull_request", (full_name, head, base, title)))
        return PullRequestInfo(
            number=1,
            url=f"https://github.com/{full_name}/pull/1",
            title=title,
            head=head,
            base=base,
        )

    async def list_pull_requests(
        self, full_name: str, state: str = "open"
    ) -> list[PullRequestInfo]:
        self.calls.append(("list_pull_requests", (full_name, state)))
        return []

    async def list_workflow_runs(self, full_name: str) -> list[WorkflowRun]:
        self.calls.append(("list_workflow_runs", (full_name,)))
        return [WorkflowRun(id=1, name="ci", status="completed", conclusion="success")]

    async def dispatch_workflow(
        self,
        full_name: str,
        workflow: str,
        ref: str,
        inputs: Optional[dict[str, Any]] = None,
    ) -> None:
        recorded = (full_name, workflow, ref, tuple(sorted((inputs or {}).items())))
        self.calls.append(("dispatch_workflow", recorded))

    async def search_code(self, query: str) -> list[dict[str, Any]]:
        self.calls.append(("search_code", (query,)))
        return []


def build_github(config) -> Optional[GitHubIntegration]:
    """Build the configured GitHub integration, or ``None`` when unconfigured.

    A GitHub App is preferred; a PAT is used only when the owner selects the
    ``pat`` method. Only one credential is ever required.
    """

    agent = config.agent
    if not agent.github_ready():
        if agent.dry_run:
            return DryRunGitHubIntegration()
        return None

    from github.auth import GitHubAppAuth, PATAuth

    if agent.github_auth == "pat":
        auth: GitHubAuth = PATAuth(agent.github_pat or "")
    else:
        auth = GitHubAppAuth(
            app_id=agent.github_app_id or "",
            installation_id=agent.github_app_installation_id or "",
            private_key_path=agent.github_app_private_key_path or "",
            api_base=agent.github_api_base,
        )
    return RestGitHubIntegration(auth, api_base=agent.github_api_base)
