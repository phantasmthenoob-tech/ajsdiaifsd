"""Configuration and owner-authorization tests."""

from __future__ import annotations

import pytest

from config import Config, ConfigError
from logging_setup import REDACTED, redact_secrets

from tests.conftest import OWNER_ID, TEST_ENV


def test_parses_valid_environment():
    config = Config.from_env(TEST_ENV, require_secrets=False, load_env_file=False)
    assert config.owner_id == OWNER_ID
    assert config.ai.provider == "echo"
    assert config.conversation.max_context_tokens == 2000
    assert config.files.max_attachment_bytes > 0


def test_missing_required_secrets_raise():
    with pytest.raises(ConfigError):
        Config.from_env({}, require_secrets=True, load_env_file=False)
    with pytest.raises(ConfigError):
        Config.from_env(
            {"DISCORD_BOT_TOKEN": "x"}, require_secrets=True, load_env_file=False
        )


def test_owner_authorization_uses_numeric_id_only():
    config = Config.from_env(TEST_ENV, require_secrets=False, load_env_file=False)
    assert config.is_owner(OWNER_ID) is True
    assert config.is_owner(str(OWNER_ID)) is True  # Discord sometimes passes strings
    assert config.is_owner(999999999999999999) is False
    assert config.is_owner(None) is False
    # A username-looking value is never treated as an ID.
    assert config.is_owner("Not_verdict_lma0") is False


def test_non_numeric_owner_id_rejected():
    with pytest.raises(ConfigError):
        Config.from_env(
            {**TEST_ENV, "BOT_OWNER_ID": "Not_verdict_lma0"},
            require_secrets=False,
            load_env_file=False,
        )


def test_invalid_numeric_values_rejected():
    with pytest.raises(ConfigError):
        Config.from_env(
            {**TEST_ENV, "AI_MAX_TOKENS": "lots"}, require_secrets=False, load_env_file=False
        )
    with pytest.raises(ConfigError):
        Config.from_env(
            {**TEST_ENV, "AI_TEMPERATURE": "3.0"}, require_secrets=False, load_env_file=False
        )


def test_secret_values_and_redaction():
    config = Config.from_env(
        {**TEST_ENV, "AI_API_KEY": "super-secret-key-123"},
        require_secrets=False,
        load_env_file=False,
    )
    secrets = config.secret_values()
    assert "super-secret-key-123" in secrets
    assert "test-token" not in secrets  # placeholder tokens are not treated as secrets
    assert redact_secrets("key=super-secret-key-123", secrets) == f"key={REDACTED}"
