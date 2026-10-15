"""GitHub authentication behind a common interface.

Both methods are supported so the owner can switch between them without changing
anything else in the application:

* :class:`GitHubAppAuth` - preferred. Signs a short-lived RS256 JWT with the App
  private key and exchanges it for a short-lived installation access token, which
  is cached and refreshed automatically.
* :class:`PATAuth` - fallback for a fine-grained personal access token.

Tokens are never returned in logs, prompts, Discord messages or generated files;
only the minute-scoped API call needs them.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Refresh an installation token this many seconds before it actually expires.
_REFRESH_SKEW = 120
# GitHub rejects JWTs with an expiry more than 10 minutes out.
_JWT_TTL = 540


class GitHubAuthError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class GitHubAuth(ABC):
    """Supplies a bearer token for GitHub API calls."""

    method: str = "unknown"

    @abstractmethod
    async def token(self) -> str:
        """Return a currently-valid token."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class PATAuth(GitHubAuth):
    """A fine-grained personal access token (fallback)."""

    method = "pat"

    def __init__(self, token: str) -> None:
        if not token:
            raise GitHubAuthError("GITHUB_TOKEN_PAT is not configured.")
        self._token = token

    async def token(self) -> str:
        return self._token


class GitHubAppAuth(GitHubAuth):
    """GitHub App installation authentication."""

    method = "app"

    def __init__(
        self,
        *,
        app_id: str,
        installation_id: str,
        private_key_path: str,
        api_base: str = "https://api.github.com",
        session: Optional[aiohttp.ClientSession] = None,
        now: Optional[callable] = None,
    ) -> None:
        if not (app_id and installation_id and private_key_path):
            raise GitHubAuthError(
                "GitHub App auth needs GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID and "
                "GITHUB_APP_PRIVATE_KEY_PATH."
            )
        self.app_id = str(app_id)
        self.installation_id = str(installation_id)
        self.api_base = api_base.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._now = now or time.time
        self._cached_token: Optional[str] = None
        self._expires_at: float = 0.0

        key_path = Path(private_key_path)
        if not key_path.is_file():
            raise GitHubAuthError(
                "The GitHub App private key file could not be found at the configured path."
            )
        self._private_key = key_path.read_text(encoding="utf-8")

    def _jwt(self) -> str:
        try:
            import jwt  # PyJWT
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise GitHubAuthError("PyJWT is required for GitHub App authentication.") from exc

        issued = int(self._now()) - 60
        payload = {"iat": issued, "exp": issued + _JWT_TTL, "iss": self.app_id}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            self._owns_session = True
        return self._session

    async def token(self) -> str:
        now = self._now()
        if self._cached_token and now < self._expires_at - _REFRESH_SKEW:
            return self._cached_token

        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {self._jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{self.api_base}/app/installations/{self.installation_id}/access_tokens"
        try:
            async with session.post(url, headers=headers) as response:
                if response.status >= 400:
                    raise GitHubAuthError(
                        f"GitHub rejected the App credentials (HTTP {response.status})."
                    )
                payload = await response.json()
        except aiohttp.ClientError as exc:
            raise GitHubAuthError("Could not reach GitHub to mint an installation token.") from exc

        token = payload.get("token")
        if not token:
            raise GitHubAuthError("GitHub did not return an installation token.")

        expires_at = payload.get("expires_at")
        self._expires_at = now + 3600
        if expires_at:
            try:
                from datetime import datetime

                self._expires_at = datetime.fromisoformat(
                    expires_at.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:  # pragma: no cover - defensive
                pass
        self._cached_token = token
        logger.info("Minted a GitHub App installation token (expires in %ss)", int(self._expires_at - now))
        return token

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
