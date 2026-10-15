"""Logging helpers that keep secrets and private content out of the logs."""

from __future__ import annotations

import logging
import sys
from typing import Iterable, Sequence

REDACTED = "***REDACTED***"


def redact_secrets(text: str, secrets: Sequence[str]) -> str:
    """Replace any occurrence of a known secret with a redaction marker."""

    if not text:
        return text
    result = text
    for secret in secrets:
        # Ignore trivially short values to avoid replacing common substrings.
        if secret and len(secret) >= 6 and secret in result:
            result = result.replace(secret, REDACTED)
    return result


class SecretRedactingFilter(logging.Filter):
    """A logging filter that scrubs registered secrets from every record."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def add_secret(self, secret: str) -> None:
        if secret and len(secret) >= 6 and secret not in self._secrets:
            self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib name
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        scrubbed = redact_secrets(message, self._secrets)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def configure_logging(
    level: str = "INFO",
    secrets: Sequence[str] = (),
    *,
    stream=None,
) -> SecretRedactingFilter:
    """Configure root logging and return the installed redacting filter."""

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove previously-installed handlers when reconfiguring (e.g. in tests).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    redactor = SecretRedactingFilter(secrets)
    handler.addFilter(redactor)
    root.addHandler(handler)

    # discord.py is chatty at DEBUG; keep it at the requested level.
    logging.getLogger("discord").setLevel(logging.INFO)
    return redactor
