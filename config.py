"""Environment-based configuration for the Discord AI chatbot.

Everything the application needs is read from environment variables (optionally
seeded from a local ``.env`` file).  This module never hard-codes credentials and
exposes :meth:`Config.secret_values` so secrets can be redacted from logs.

The configuration is intentionally a set of immutable dataclasses so it is easy
to unit-test by constructing :meth:`Config.from_env` with an explicit mapping.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

try:  # python-dotenv is optional at import time so tests never need a .env file.
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - only hit when dependency is absent
    def load_dotenv(*_args, **_kwargs) -> bool:  # type: ignore[misc]
        return False


class ConfigError(ValueError):
    """Raised when required configuration is missing or malformed."""


# ---------------------------------------------------------------------------
# Small, well-tested parsing helpers
# ---------------------------------------------------------------------------
def _read(env: Mapping[str, str], name: str) -> Optional[str]:
    raw = env.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _get_str(env: Mapping[str, str], name: str, default: Optional[str] = None) -> Optional[str]:
    return _read(env, name) or default


def _get_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _read(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _get_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _read(env, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _get_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _read(env, name)
    if raw is None:
        return default
    low = raw.lower()
    if low in {"1", "true", "yes", "y", "on"}:
        return True
    if low in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _get_str_list(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = _read(env, name)
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _get_int_list(env: Mapping[str, str], name: str) -> tuple[int, ...]:
    raw = _read(env, name)
    if not raw:
        return ()
    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(int(part))
        except ValueError as exc:
            raise ConfigError(f"{name} must be a comma-separated list of integers") from exc
    return tuple(values)


# ---------------------------------------------------------------------------
# Configuration sections
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AIConfig:
    """Provider-independent AI settings."""

    provider: str
    model: str
    api_key: str
    base_url: Optional[str]
    temperature: float
    max_tokens: int
    timeout: float
    max_retries: int
    api_version: Optional[str] = None

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ConfigError("AI_MAX_TOKENS must be positive")
        if self.timeout <= 0:
            raise ConfigError("AI_TIMEOUT must be positive")
        if self.max_retries < 0:
            raise ConfigError("AI_MAX_RETRIES must not be negative")
        if not 0.0 <= self.temperature <= 2.0:
            raise ConfigError("AI_TEMPERATURE must be between 0.0 and 2.0")


@dataclass(frozen=True)
class FileLimits:
    """Upper bounds applied to every file the bot reads, edits or generates."""

    max_attachment_bytes: int = 5_000_000
    max_archive_bytes: int = 20_000_000
    max_archive_uncompressed_bytes: int = 100_000_000
    max_archive_files: int = 2000
    max_archive_depth: int = 10
    max_archive_compression_ratio: int = 200
    max_generated_files: int = 40
    max_generated_file_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        for name in (
            "max_attachment_bytes",
            "max_archive_bytes",
            "max_archive_uncompressed_bytes",
            "max_archive_files",
            "max_archive_depth",
            "max_archive_compression_ratio",
            "max_generated_files",
            "max_generated_file_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name} must be positive")


@dataclass(frozen=True)
class SandboxConfig:
    """Isolated-execution settings. Disabled by default."""

    enabled: bool = False
    backend: str = "none"
    timeout: int = 10
    memory_mb: int = 256
    cpu_limit: float = 1.0


@dataclass(frozen=True)
class ConversationConfig:
    """Limits governing memory, token budgets and activation."""

    max_history_messages: int = 20
    max_context_tokens: int = 8000
    summarize_after_messages: int = 40
    retention_days: int = 0
    enable_reply_conversations: bool = True
    strict_mention: bool = True

    def __post_init__(self) -> None:
        if self.max_history_messages <= 0:
            raise ConfigError("MAX_HISTORY_MESSAGES must be positive")
        if self.max_context_tokens <= 0:
            raise ConfigError("MAX_CONTEXT_TOKENS must be positive")
        if self.retention_days < 0:
            raise ConfigError("CONTEXT_RETENTION_DAYS must not be negative")


@dataclass(frozen=True)
class InfraConfig:
    """Infrastructure control-plane settings.

    Disabled and in dry-run mode by default: no provider token is required and
    no real infrastructure is created until the owner explicitly enables it.
    """

    enabled: bool = False
    dry_run: bool = True
    render_api_key: Optional[str] = None
    render_owner_id: Optional[str] = None
    fly_api_token: Optional[str] = None
    fly_app: Optional[str] = None
    max_targets: int = 5
    max_concurrent_deploys: int = 2
    name_pattern: str = r"^[a-z0-9][a-z0-9-]{1,40}$"
    git_remote: Optional[str] = None
    git_branch: str = "main"
    docker_registry: Optional[str] = None
    docker_image_prefix: str = "discord-bot"
    health_port: Optional[int] = None

    def __post_init__(self) -> None:
        if self.max_targets < 1:
            raise ConfigError("INFRA_MAX_TARGETS must be at least 1")
        if self.max_concurrent_deploys < 1:
            raise ConfigError("INFRA_MAX_CONCURRENT_DEPLOYS must be at least 1")

    def provider_ready(self, kind: str) -> bool:
        """Return whether a real provider has enough configuration to be used."""

        if kind == "render":
            return bool(self.render_api_key)
        if kind == "fly":
            return bool(self.fly_api_token and self.fly_app)
        return False


@dataclass(frozen=True)
class AgentConfig:
    """Autonomy limits, integrations and publishing mode for the agent."""

    # -- autonomy bounds (always enforced, independent of the model) --------
    task_deadline_seconds: int = 900
    max_tool_calls: int = 40
    max_step_attempts: int = 3
    max_concurrent_tasks: int = 2
    approval_ttl_seconds: int = 3600

    # -- GitHub -------------------------------------------------------------
    github_auth: str = "app"  # 'app' (preferred) or 'pat' (fallback)
    github_app_id: Optional[str] = None
    github_app_installation_id: Optional[str] = None
    github_app_private_key_path: Optional[str] = None
    github_pat: Optional[str] = None
    github_api_base: str = "https://api.github.com"
    github_allowed_repos: tuple[str, ...] = ()

    # -- VM execution worker -------------------------------------------------
    worker_url: Optional[str] = None
    worker_token: Optional[str] = None
    worker_timeout: float = 120.0

    # -- Surge static hosting ------------------------------------------------
    surge_token: Optional[str] = None
    surge_allowed_domains: tuple[str, ...] = ()

    # -- Safety --------------------------------------------------------------
    # When True, every mutating tool is executed in dry-run mode. Real writes
    # only happen once the owner explicitly opts in.
    dry_run: bool = True

    def __post_init__(self) -> None:
        if self.task_deadline_seconds <= 0:
            raise ConfigError("AGENT_TASK_DEADLINE must be positive")
        if self.max_tool_calls <= 0:
            raise ConfigError("AGENT_MAX_TOOL_CALLS must be positive")
        if self.max_step_attempts < 1:
            raise ConfigError("AGENT_MAX_STEP_ATTEMPTS must be at least 1")
        if self.approval_ttl_seconds <= 0:
            raise ConfigError("AGENT_APPROVAL_TTL must be positive")
        if self.github_auth not in {"app", "pat"}:
            raise ConfigError("GITHUB_AUTH must be either 'app' or 'pat'")

    def github_ready(self) -> bool:
        if self.github_auth == "pat":
            return bool(self.github_pat)
        return bool(
            self.github_app_id
            and self.github_app_installation_id
            and self.github_app_private_key_path
        )


@dataclass(frozen=True)
class Config:
    """The fully-resolved application configuration."""

    discord_token: str
    owner_id: int
    ai: AIConfig
    files: FileLimits
    sandbox: SandboxConfig
    conversation: ConversationConfig
    infra: InfraConfig
    agent: AgentConfig
    database_url: str
    guild_ids: tuple[int, ...] = ()
    log_level: str = "INFO"
    log_private_content: bool = False

    # -- construction -------------------------------------------------------
    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        require_secrets: bool = True,
        load_env_file: bool = True,
    ) -> "Config":
        """Build a :class:`Config` from environment variables.

        Parameters
        ----------
        env:
            Explicit mapping (defaults to ``os.environ``).
        require_secrets:
            When ``False`` placeholder values are tolerated, which is handy for
            tests and tooling that never talks to Discord or an AI provider.
        load_env_file:
            When ``True`` (and no explicit ``env`` mapping is passed) a local
            ``.env`` file is loaded first.
        """

        if env is None:
            if load_env_file:
                load_dotenv(override=False)
            env = os.environ

        token = _get_str(env, "DISCORD_BOT_TOKEN")
        owner_raw = _get_str(env, "BOT_OWNER_ID")

        if require_secrets:
            if not token:
                raise ConfigError(
                    "DISCORD_BOT_TOKEN is required. Set it in your environment or .env file."
                )
            if not owner_raw:
                raise ConfigError(
                    "BOT_OWNER_ID is required. Set it to the owner's numeric Discord user ID."
                )
        else:
            token = token or "test-token"
            owner_raw = owner_raw or "0"

        try:
            owner_id = int(owner_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ConfigError("BOT_OWNER_ID must be a numeric Discord user ID") from exc
        if owner_id <= 0 and require_secrets:
            raise ConfigError("BOT_OWNER_ID must be a positive numeric Discord user ID")

        ai_key = _get_str(env, "AI_API_KEY") or ""
        if require_secrets and not ai_key:
            raise ConfigError("AI_API_KEY is required (keep it in the environment, never in code).")

        ai = AIConfig(
            provider=(_get_str(env, "AI_PROVIDER", "openai") or "openai").lower(),
            model=_get_str(env, "AI_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
            api_key=ai_key,
            base_url=_get_str(env, "AI_BASE_URL"),
            temperature=_get_float(env, "AI_TEMPERATURE", 0.7),
            max_tokens=_get_int(env, "AI_MAX_TOKENS", 2048),
            timeout=_get_float(env, "AI_TIMEOUT", 60.0),
            max_retries=_get_int(env, "AI_MAX_RETRIES", 3),
            api_version=_get_str(env, "AI_API_VERSION"),
        )

        files = FileLimits(
            max_attachment_bytes=_get_int(env, "MAX_ATTACHMENT_BYTES", 5_000_000),
            max_archive_bytes=_get_int(env, "MAX_ARCHIVE_BYTES", 20_000_000),
            max_archive_uncompressed_bytes=_get_int(
                env, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 100_000_000
            ),
            max_archive_files=_get_int(env, "MAX_ARCHIVE_FILES", 2000),
            max_archive_depth=_get_int(env, "MAX_ARCHIVE_DEPTH", 10),
            max_archive_compression_ratio=_get_int(env, "MAX_ARCHIVE_COMPRESSION_RATIO", 200),
            max_generated_files=_get_int(env, "MAX_GENERATED_FILES", 40),
            max_generated_file_bytes=_get_int(env, "MAX_GENERATED_FILE_BYTES", 2_000_000),
        )

        sandbox = SandboxConfig(
            enabled=_get_bool(env, "ENABLE_SANDBOX_EXECUTION", False),
            backend=(_get_str(env, "SANDBOX_BACKEND", "none") or "none").lower(),
            timeout=_get_int(env, "SANDBOX_TIMEOUT", 10),
            memory_mb=_get_int(env, "SANDBOX_MEMORY_MB", 256),
            cpu_limit=_get_float(env, "SANDBOX_CPU_LIMIT", 1.0),
        )

        conversation = ConversationConfig(
            max_history_messages=_get_int(env, "MAX_HISTORY_MESSAGES", 20),
            max_context_tokens=_get_int(env, "MAX_CONTEXT_TOKENS", 8000),
            summarize_after_messages=_get_int(env, "SUMMARIZE_AFTER_MESSAGES", 40),
            retention_days=_get_int(env, "CONTEXT_RETENTION_DAYS", 0),
            enable_reply_conversations=_get_bool(env, "ENABLE_REPLY_CONVERSATIONS", True),
            strict_mention=_get_bool(env, "STRICT_MENTION", True),
        )

        health_port = _get_int(env, "HEALTH_PORT", _get_int(env, "PORT", 0)) or None
        infra = InfraConfig(
            enabled=_get_bool(env, "INFRA_ENABLED", False),
            dry_run=_get_bool(env, "INFRA_DRY_RUN", True),
            render_api_key=_get_str(env, "RENDER_API_KEY"),
            render_owner_id=_get_str(env, "RENDER_OWNER_ID"),
            fly_api_token=_get_str(env, "FLY_API_TOKEN"),
            fly_app=_get_str(env, "FLY_APP_NAME"),
            max_targets=_get_int(env, "INFRA_MAX_TARGETS", 5),
            max_concurrent_deploys=_get_int(env, "INFRA_MAX_CONCURRENT_DEPLOYS", 2),
            name_pattern=_get_str(env, "INFRA_NAME_PATTERN", r"^[a-z0-9][a-z0-9-]{1,40}$")
            or r"^[a-z0-9][a-z0-9-]{1,40}$",
            git_remote=_get_str(env, "INFRA_GIT_REMOTE"),
            git_branch=_get_str(env, "INFRA_GIT_BRANCH", "main") or "main",
            docker_registry=_get_str(env, "DOCKER_REGISTRY"),
            docker_image_prefix=_get_str(env, "DOCKER_IMAGE_PREFIX", "discord-bot") or "discord-bot",
            health_port=health_port,
        )

        agent = AgentConfig(
            task_deadline_seconds=_get_int(env, "AGENT_TASK_DEADLINE", 900),
            max_tool_calls=_get_int(env, "AGENT_MAX_TOOL_CALLS", 40),
            max_step_attempts=_get_int(env, "AGENT_MAX_STEP_ATTEMPTS", 3),
            max_concurrent_tasks=_get_int(env, "AGENT_MAX_CONCURRENT_TASKS", 2),
            approval_ttl_seconds=_get_int(env, "AGENT_APPROVAL_TTL", 3600),
            github_auth=(_get_str(env, "GITHUB_AUTH", "app") or "app").lower(),
            github_app_id=_get_str(env, "GITHUB_APP_ID"),
            github_app_installation_id=_get_str(env, "GITHUB_APP_INSTALLATION_ID"),
            github_app_private_key_path=_get_str(env, "GITHUB_APP_PRIVATE_KEY_PATH"),
            github_pat=_get_str(env, "GITHUB_TOKEN_PAT"),
            github_api_base=_get_str(env, "GITHUB_API_BASE", "https://api.github.com")
            or "https://api.github.com",
            github_allowed_repos=_get_str_list(env, "GITHUB_ALLOWED_REPOS"),
            worker_url=_get_str(env, "WORKER_URL"),
            worker_token=_get_str(env, "WORKER_TOKEN"),
            worker_timeout=_get_float(env, "WORKER_TIMEOUT", 120.0),
            surge_token=_get_str(env, "SURGE_TOKEN"),
            surge_allowed_domains=_get_str_list(env, "SURGE_ALLOWED_DOMAINS"),
            dry_run=_get_bool(env, "AGENT_DRY_RUN", True),
        )

        return cls(
            discord_token=token,  # type: ignore[arg-type]
            owner_id=owner_id,
            ai=ai,
            files=files,
            sandbox=sandbox,
            conversation=conversation,
            infra=infra,
            agent=agent,
            database_url=_get_str(env, "DATABASE_URL", "sqlite:///data/bot.db") or "sqlite:///data/bot.db",
            guild_ids=_get_int_list(env, "DISCORD_GUILD_IDS"),
            log_level=(_get_str(env, "LOG_LEVEL", "INFO") or "INFO").upper(),
            log_private_content=_get_bool(env, "LOG_PRIVATE_CONTENT", False),
        )

    # -- authorization ------------------------------------------------------
    def is_owner(self, user_id: Optional[int]) -> bool:
        """Return ``True`` only for the configured numeric owner ID.

        Authorization *never* considers usernames, nicknames, display names or
        role names, because all of those can change.  Only the immutable numeric
        Discord ID is compared.
        """

        if user_id is None:
            return False
        try:
            return int(user_id) == int(self.owner_id)
        except (TypeError, ValueError):
            return False

    # -- secret handling ----------------------------------------------------
    def secret_values(self) -> list[str]:
        """Return every secret string so it can be redacted from logs."""

        secrets = [
            self.discord_token,
            self.ai.api_key,
            self.infra.render_api_key,
            self.infra.fly_api_token,
            self.agent.github_pat,
            self.agent.worker_token,
            self.agent.surge_token,
        ]
        return [s for s in secrets if s and s not in {"test-token"}]


def load_config(
    env: Optional[Mapping[str, str]] = None,
    *,
    require_secrets: bool = True,
    load_env_file: bool = True,
) -> Config:
    """Convenience wrapper around :meth:`Config.from_env`."""

    return Config.from_env(env, require_secrets=require_secrets, load_env_file=load_env_file)
