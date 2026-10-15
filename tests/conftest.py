"""Shared fixtures and lightweight test doubles."""

from __future__ import annotations

from typing import Optional, Sequence

import pytest
import pytest_asyncio

from ai.provider import AIProvider, ChatMessage, CompletionResult
from config import Config
from context.repository import (
    ContextRepository,
    ConversationRepository,
    FileMetadataRepository,
)
from context.service import ContextService
from agent.store import TaskStore
from agent.targets import TrustedTargetStore
from approvals.service import ApprovalService
from database.database import Database
from engine import ChatbotEngine
from infra.registry import TargetRegistry
from infra.service import InfraService, build_infra_service
from permissions.service import PermissionService
from permissions.store import PermissionStore
from sandbox.runner import StaticValidator

OWNER_ID = 111111111111111111

TEST_ENV = {
    "DISCORD_BOT_TOKEN": "test-token",
    "BOT_OWNER_ID": str(OWNER_ID),
    "AI_PROVIDER": "echo",
    "AI_MODEL": "echo",
    "AI_API_KEY": "test-key",
    "DATABASE_URL": ":memory:",
    "AI_MAX_TOKENS": "512",
    "MAX_CONTEXT_TOKENS": "2000",
}

# Infrastructure plane enabled but in dry-run mode, with a small target quota.
INFRA_ENV = {
    **TEST_ENV,
    "INFRA_ENABLED": "true",
    "INFRA_DRY_RUN": "true",
    "INFRA_MAX_TARGETS": "2",
    "INFRA_GIT_REMOTE": "https://example.invalid/repo.git",
    "INFRA_GIT_BRANCH": "main",
}


class ScriptedProvider(AIProvider):
    """Returns queued responses and records the messages it received."""

    name = "scripted"

    def __init__(self, responses: Optional[Sequence[str]] = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[list[ChatMessage]] = []

    async def complete(self, messages, *, model=None, temperature=None, max_tokens=None):
        self.calls.append(list(messages))
        text = self.responses.pop(0) if self.responses else "acknowledged"
        return CompletionResult(text=text, model=model or "scripted")


class FailingProvider(AIProvider):
    name = "failing"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def complete(self, messages, *, model=None, temperature=None, max_tokens=None):
        raise self.error


@pytest.fixture
def config() -> Config:
    return Config.from_env(TEST_ENV, require_secrets=False, load_env_file=False)


@pytest.fixture
def infra_config() -> Config:
    return Config.from_env(INFRA_ENV, require_secrets=False, load_env_file=False)


@pytest_asyncio.fixture
async def db() -> Database:
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def context_repository(db: Database) -> ContextRepository:
    return ContextRepository(db)


@pytest_asyncio.fixture
async def conversation_repository(db: Database) -> ConversationRepository:
    return ConversationRepository(db)


@pytest_asyncio.fixture
async def file_metadata_repository(db: Database) -> FileMetadataRepository:
    return FileMetadataRepository(db)


@pytest_asyncio.fixture
async def context_service(context_repository: ContextRepository) -> ContextService:
    return ContextService(context_repository)


@pytest_asyncio.fixture
async def permission_service(config: Config, db: Database) -> PermissionService:
    return PermissionService(config, PermissionStore(db))


@pytest_asyncio.fixture
async def approval_service(config: Config, db: Database) -> ApprovalService:
    return ApprovalService(config, db)


@pytest_asyncio.fixture
async def trusted_targets(db: Database) -> TrustedTargetStore:
    return TrustedTargetStore(db)


@pytest_asyncio.fixture
async def task_store(db: Database) -> TaskStore:
    return TaskStore(db)


@pytest_asyncio.fixture
async def infra_registry(db: Database) -> TargetRegistry:
    return TargetRegistry(db)


@pytest_asyncio.fixture
async def infra_service(infra_config: Config, infra_registry: TargetRegistry) -> InfraService:
    return build_infra_service(infra_config, infra_registry)


@pytest_asyncio.fixture
async def engine(
    config: Config,
    context_service: ContextService,
    conversation_repository: ConversationRepository,
    file_metadata_repository: FileMetadataRepository,
) -> ChatbotEngine:
    return ChatbotEngine(
        config=config,
        provider=ScriptedProvider(),
        context_service=context_service,
        conversations=conversation_repository,
        file_metadata=file_metadata_repository,
        validator=StaticValidator(allow_subprocess=False),
    )
