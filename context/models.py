"""Domain models for context entries and conversation history."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Scope(str, Enum):
    """Where a context entry applies."""

    ALL = "all"
    USER = "user"
    CHANNEL = "channel"
    SERVER = "server"

    @classmethod
    def parse(cls, value: str) -> "Scope":
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"Unknown scope: {value!r}") from exc


class Role(str, Enum):
    """Message roles.

    ``user`` and ``assistant`` are the only roles an owner may store through the
    context commands.  ``system`` and ``developer`` are reserved for
    application- and developer-level instructions and are never user-settable,
    keeping security instructions separate from stored content.
    """

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    DEVELOPER = "developer"

    @classmethod
    def parse(cls, value: str) -> "Role":
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"Unknown role: {value!r}") from exc


class Operation(str, Enum):
    """Mutation applied by ``/setcontext``."""

    APPEND = "append"
    REPLACE = "replace"
    REMOVE = "remove"

    @classmethod
    def parse(cls, value: str) -> "Operation":
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"Unknown operation: {value!r}") from exc


@dataclass
class ContextEntry:
    """A single stored context entry."""

    scope: Scope
    role: Role
    content: str
    target_id: Optional[int] = None
    label: Optional[str] = None
    priority: int = 0
    created_by: Optional[int] = None
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope.value,
            "target_id": str(self.target_id) if self.target_id is not None else None,
            "role": self.role.value,
            "content": self.content,
            "label": self.label,
            "priority": self.priority,
        }


@dataclass
class Conversation:
    """A conversation bucket, unique per (guild, channel, user, kind)."""

    scope_key: str
    kind: str
    guild_id: Optional[int] = None
    channel_id: Optional[int] = None
    user_id: Optional[int] = None
    summary: Optional[str] = None
    summary_upto: int = 0
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class StoredMessage:
    """A persisted conversation message."""

    conversation_id: int
    role: Role
    content: str
    author_id: Optional[int] = None
    author_name: Optional[str] = None
    token_estimate: int = 0
    id: Optional[int] = None
    created_at: Optional[str] = None


@dataclass
class FileRecord:
    """Metadata about a file seen during a conversation."""

    direction: str
    filename: str
    mime_type: Optional[str] = None
    size_bytes: int = 0
    sha256: Optional[str] = None
    conversation_id: Optional[int] = None
    id: Optional[int] = None
    created_at: Optional[str] = None
