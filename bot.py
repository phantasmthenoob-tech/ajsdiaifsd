"""Discord AI Chatbot entry point.

Run with ``python bot.py`` (or ``python -m bot``).  Startup sequence:

1. Load and validate configuration from the environment.
2. Configure logging with secret redaction.
3. Open the database and apply migrations.
4. Build the AI provider, sandbox, context service and engine.
5. Start the Discord client (which syncs slash commands and serves requests).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from ai.provider import create_provider
from config import Config, ConfigError, load_config
from context.repository import (
    ContextRepository,
    ConversationRepository,
    FileMetadataRepository,
)
from context.service import ContextService
from database.database import Database
from agent.runtime import build_agent_runtime
from discord_client import DiscordChatbot
from engine import ChatbotEngine
from health import start_health_server
from infra.registry import TargetRegistry
from infra.service import build_infra_service
from logging_setup import configure_logging
from sandbox.runner import StaticValidator, create_sandbox

logger = logging.getLogger(__name__)


async def _purge_expired(config: Config, conversations: ConversationRepository) -> int:
    """Apply the retention policy, if configured."""

    days = config.conversation.retention_days
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    removed = await conversations.purge_older_than(cutoff)
    if removed:
        logger.info("Purged %d conversations older than %d day(s)", removed, days)
    return removed


async def _run(config: Config) -> None:
    database = Database(config.database_url)
    await database.initialize()

    context_repository = ContextRepository(database)
    conversations = ConversationRepository(database)
    file_metadata = FileMetadataRepository(database)
    context_service = ContextService(context_repository)
    infra_service = build_infra_service(config, TargetRegistry(database))
    agent_runtime = build_agent_runtime(config, database, infra=infra_service)
    recovered = await agent_runtime.orchestrator.recover()
    if recovered:
        logger.info("Recovered %d interrupted task(s) for resumption", len(recovered))

    provider = create_provider(config.ai)
    sandbox = create_sandbox(config.sandbox)
    engine = ChatbotEngine(
        config=config,
        provider=provider,
        context_service=context_service,
        conversations=conversations,
        file_metadata=file_metadata,
        sandbox=sandbox,
        validator=StaticValidator(),
    )

    await _purge_expired(config, conversations)

    if not config.sandbox.enabled:
        logger.info("Sandbox execution disabled: static syntax validation only.")

    chatbot = DiscordChatbot(
        config=config,
        engine=engine,
        context_service=context_service,
        infra_service=infra_service,
        agent_runtime=agent_runtime,
    )

    health_runner = None
    if config.infra.health_port:
        health_runner = await start_health_server(config.infra.health_port)
        logger.info("Health endpoint enabled for uptime monitoring")
    else:
        logger.info("No PORT set; running as a plain gateway bot (no health endpoint)")

    try:
        await chatbot.bot.start(config.discord_token)
    finally:
        if health_runner is not None:
            await health_runner.cleanup()
        await agent_runtime.close()
        await infra_service.close()
        await provider.close()
        await database.close()


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        # Print to stderr; there is nothing to redact yet.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level, config.secret_values())
    logger.info("Starting Discord AI Chatbot (provider=%s, model=%s)", config.ai.provider, config.ai.model)

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("Shutting down on keyboard interrupt")
    except Exception:  # pragma: no cover - top-level guard
        logger.exception("Fatal error during startup")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
