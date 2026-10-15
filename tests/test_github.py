"""GitHub integration tests: App JWT minting, PAT, request shapes, dry run."""

from __future__ import annotations

import pytest

from config import Config
from github.auth import GitHubAppAuth, GitHubAuthError, PATAuth
from github.client import DryRunGitHubIntegration, RestGitHubIntegration, build_github

from tests.conftest import TEST_ENV


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def json(self):
        import json

        return json.loads(self._body)

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

    def post(self, url, headers=None):
        self.calls.append(("POST", url, headers, None))
        return self._responses.pop(0)


async def test_pat_auth_returns_token():
    auth = PATAuth("ghp_example")
    assert await auth.token() == "ghp_example"


def test_pat_auth_requires_token():
    with pytest.raises(GitHubAuthError):
        PATAuth("")


def _write_key(tmp_path) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "app-key.pem"
    path.write_bytes(pem)
    return str(path)


async def test_github_app_mints_and_caches_installation_token(tmp_path):
    key_path = _write_key(tmp_path)
    session = FakeSession(
        [FakeResponse(201, '{"token":"ghs_installation","expires_at":"2099-01-01T00:00:00Z"}')]
    )
    auth = GitHubAppAuth(
        app_id="123",
        installation_id="456",
        private_key_path=key_path,
        session=session,
    )
    token = await auth.token()
    assert token == "ghs_installation"

    # The JWT sent to GitHub must be a valid RS256 token issued by the App.
    import jwt

    headers = session.calls[0][2]
    raw = headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(raw, options={"verify_signature": False})
    assert claims["iss"] == "123"

    # Second call is served from cache.
    assert await auth.token() == "ghs_installation"
    assert len(session.calls) == 1


async def test_github_app_requires_configuration(tmp_path):
    with pytest.raises(GitHubAuthError):
        GitHubAppAuth(app_id="", installation_id="", private_key_path="")


async def test_rest_client_request_shapes():
    session = FakeSession(
        [
            FakeResponse(200, '{"full_name":"owner/repo","default_branch":"main"}'),
            FakeResponse(200, '{"object":{"sha":"abc123"}}'),
            FakeResponse(201, '{"ref":"refs/heads/agent/x"}'),
        ]
    )
    client = RestGitHubIntegration(PATAuth("t"), session=session)

    repo = await client.get_repo("owner/repo")
    assert repo["default_branch"] == "main"
    sha = await client.get_branch_sha("owner/repo", "main")
    assert sha == "abc123"
    await client.create_branch("owner/repo", "agent/x", sha)

    method, url, headers, body = session.calls[2]
    assert method == "POST" and url.endswith("/repos/owner/repo/git/refs")
    assert body["ref"] == "refs/heads/agent/x"
    assert headers["Authorization"] == "Bearer t"
    assert client.method == "pat"


async def test_rest_client_put_files_creates_and_updates():
    session = FakeSession(
        [
            FakeResponse(404, ""),  # no existing file
            FakeResponse(200, '{"commit":{"sha":"c1"}}'),
            FakeResponse(200, '{"sha":"existing"}'),  # file exists
            FakeResponse(200, '{"commit":{"sha":"c2"}}'),
        ]
    )
    client = RestGitHubIntegration(PATAuth("t"), session=session)
    sha = await client.put_files(
        "owner/repo", "agent/x", {"a.py": "print(1)\n", "b.py": "print(2)\n"}, "msg"
    )
    assert sha == "c2"
    # The update call must include the existing blob sha.
    update_body = session.calls[3][3]
    assert update_body["sha"] == "existing"


async def test_rest_client_search_and_workflow_runs():
    session = FakeSession(
        [
            FakeResponse(200, '{"items":[{"path":"a.py"}]}'),
            FakeResponse(
                200,
                '{"workflow_runs":[{"id":7,"name":"ci","status":"completed",'
                '"conclusion":"success","html_url":"https://x/7"}]}',
            ),
        ]
    )
    client = RestGitHubIntegration(PATAuth("t"), session=session)
    items = await client.search_code("print")
    assert items[0]["path"] == "a.py"
    runs = await client.list_workflow_runs("owner/repo")
    assert runs[0].conclusion == "success"


async def test_rest_client_dispatch_workflow():
    session = FakeSession([FakeResponse(204, "")])
    client = RestGitHubIntegration(PATAuth("t"), session=session)
    await client.dispatch_workflow("owner/repo", "ci.yml", "main", {"env": "prod"})

    method, url, _, body = session.calls[0]
    assert method == "POST"
    assert url.endswith("/repos/owner/repo/actions/workflows/ci.yml/dispatches")
    assert body == {"ref": "main", "inputs": {"env": "prod"}}


async def test_dispatch_workflow_without_inputs():
    session = FakeSession([FakeResponse(204, "")])
    client = RestGitHubIntegration(PATAuth("t"), session=session)
    await client.dispatch_workflow("owner/repo", "ci.yml", "dev")
    assert session.calls[0][3] == {"ref": "dev"}


async def test_dry_run_integration_is_inert():
    client = DryRunGitHubIntegration()
    await client.get_repo("owner/repo")
    sha = await client.get_branch_sha("owner/repo", "main")
    await client.create_branch("owner/repo", "agent/x", sha)
    await client.put_files("owner/repo", "agent/x", {"a.py": "x"}, "msg")
    await client.dispatch_workflow("owner/repo", "ci.yml", "agent/x")
    pr = await client.create_pull_request("owner/repo", "agent/x", "main", "t")
    assert pr.number == 1
    assert [c[0] for c in client.calls][:5] == [
        "get_repo",
        "get_branch_sha",
        "create_branch",
        "put_files",
        "dispatch_workflow",
    ]


def test_build_github_prefers_dry_run_when_unconfigured():
    dry = build_github(Config.from_env(TEST_ENV, require_secrets=False, load_env_file=False))
    assert isinstance(dry, DryRunGitHubIntegration)

    live = build_github(
        Config.from_env(
            {**TEST_ENV, "AGENT_DRY_RUN": "false"},
            require_secrets=False,
            load_env_file=False,
        )
    )
    assert live is None


def test_build_github_uses_pat_when_selected():
    config = Config.from_env(
        {
            **TEST_ENV,
            "AGENT_DRY_RUN": "false",
            "GITHUB_AUTH": "pat",
            "GITHUB_TOKEN_PAT": "ghp_x",
        },
        require_secrets=False,
        load_env_file=False,
    )
    client = build_github(config)
    assert isinstance(client, RestGitHubIntegration)
    assert client.method == "pat"
