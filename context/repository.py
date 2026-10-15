"""Database access for context entries, conversations and file metadata."""

from __future__ import annotations

from typing import Any, Optional, Sequence

import aiosqlite

from context.models import (
    ContextEntry,
    Conversation,
    FileRecord,
    Role,
    Scope,
    StoredMessage,
)
from database.database import Database
from utils import utcnow_iso

_UNSET: Any = object()


def _row_to_entry(row: aiosqlite.Row) -> ContextEntry:
    return ContextEntry(
        id=row["id"],
        scope=Scope(row["scope"]),
        target_id=int(row["target_id"]) if row["target_id"] is not None else None,
        role=Role(row["role"]),
        content=row["content"],
        label=row["label"],
        priority=row["priority"],
        created_by=int(row["created_by"]) if row["created_by"] is not None else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_conversation(row: aiosqlite.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        scope_key=row["scope_key"],
        kind=row["kind"],
        guild_id=int(row["guild_id"]) if row["guild_id"] is not None else None,
        channel_id=int(row["channel_id"]) if row["channel_id"] is not None else None,
        user_id=int(row["user_id"]) if row["user_id"] is not None else None,
        summary=row["summary"],
        summary_upto=row["summary_upto"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_message(row: aiosqlite.Row) -> StoredMessage:
    return StoredMessage(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=Role(row["role"]),
        author_id=int(row["author_id"]) if row["author_id"] is not None else None,
        author_name=row["author_name"],
        content=row["content"],
        token_estimate=row["token_estimate"],
        created_at=row["created_at"],
    )


class ContextRepository:
    """CRUD operations for :class:`ContextEntry` rows."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add_entry(
        self,
        *,
        scope: Scope,
        role: Role,
        content: str,
        target_id: Optional[int] = None,
        label: Optional[str] = None,
        priority: int = 0,
        created_by: Optional[int] = None,
    ) -> int:
        now = utcnow_iso()
        return await self.db.execute(
            """
            INSERT INTO context_entries
                (scope, target_id, role, content, label, priority, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope.value,
                str(target_id) if target_id is not None else None,
                role.value,
                content,
                label,
                priority,
                str(created_by) if created_by is not None else None,
                now,
                now,
            ),
        )

    async def get_entry(self, entry_id: int) -> Optional[ContextEntry]:
        row = await self.db.fetchone("SELECT * FROM context_entries WHERE id = ?", (entry_id,))
        return _row_to_entry(row) if row else None

    async def list_entries(
        self,
        *,
        scope: Optional[Scope] = None,
        target_id: Any = _UNSET,
        limit: int = 200,
        offset: int = 0,
    ) -> list[ContextEntry]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope.value)
        if target_id is not _UNSET:
            if target_id is None:
                clauses.append("target_id IS NULL")
            else:
                clauses.append("target_id = ?")
                params.append(str(target_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, limit), max(0, offset)])
        rows = await self.db.fetchall(
            f"SELECT * FROM context_entries {where} ORDER BY priority DESC, id ASC LIMIT ? OFFSET ?",
            params,
        )
        return [_row_to_entry(r) for r in rows]

    async def list_by_label(
        self,
        *,
        scope: Scope,
        target_id: Optional[int],
        label: str,
    ) -> list[ContextEntry]:
        if target_id is None:
            rows = await self.db.fetchall(
                "SELECT * FROM context_entries WHERE scope = ? AND target_id IS NULL AND label = ? ORDER BY id ASC",
                (scope.value, label),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM context_entries WHERE scope = ? AND target_id = ? AND label = ? ORDER BY id ASC",
                (scope.value, str(target_id), label),
            )
        return [_row_to_entry(r) for r in rows]

    async def update_entry(
        self,
        entry_id: int,
        *,
        content: Optional[str] = None,
        role: Optional[Role] = None,
        label: Any = _UNSET,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if content is not None:
            sets.append("content = ?")
            params.append(content)
        if role is not None:
            sets.append("role = ?")
            params.append(role.value)
        if label is not _UNSET:
            sets.append("label = ?")
            params.append(label)
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.append(utcnow_iso())
        params.append(entry_id)
        await self.db.execute(
            f"UPDATE context_entries SET {', '.join(sets)} WHERE id = ?", params
        )
        return True

    async def delete_entry(self, entry_id: int) -> bool:
        row = await self.db.fetchone("SELECT id FROM context_entries WHERE id = ?", (entry_id,))
        if not row:
            return False
        await self.db.execute("DELETE FROM context_entries WHERE id = ?", (entry_id,))
        return True

    async def delete_by_label(self, *, scope: Scope, target_id: Optional[int], label: str) -> int:
        if target_id is None:
            row = await self.db.fetchone(
                "SELECT COUNT(*) AS c FROM context_entries WHERE scope = ? AND target_id IS NULL AND label = ?",
                (scope.value, label),
            )
            await self.db.execute(
                "DELETE FROM context_entries WHERE scope = ? AND target_id IS NULL AND label = ?",
                (scope.value, label),
            )
        else:
            row = await self.db.fetchone(
                "SELECT COUNT(*) AS c FROM context_entries WHERE scope = ? AND target_id = ? AND label = ?",
                (scope.value, str(target_id), label),
            )
            await self.db.execute(
                "DELETE FROM context_entries WHERE scope = ? AND target_id = ? AND label = ?",
                (scope.value, str(target_id), label),
            )
        return int(row["c"]) if row else 0

    async def clear(self, *, scope: Scope, target_id: Optional[int]) -> int:
        if target_id is None:
            row = await self.db.fetchone(
                "SELECT COUNT(*) AS c FROM context_entries WHERE scope = ? AND target_id IS NULL",
                (scope.value,),
            )
            await self.db.execute(
                "DELETE FROM context_entries WHERE scope = ? AND target_id IS NULL", (scope.value,)
            )
        else:
            row = await self.db.fetchone(
                "SELECT COUNT(*) AS c FROM context_entries WHERE scope = ? AND target_id = ?",
                (scope.value, str(target_id)),
            )
            await self.db.execute(
                "DELETE FROM context_entries WHERE scope = ? AND target_id = ?",
                (scope.value, str(target_id)),
            )
        return int(row["c"]) if row else 0

    async def count(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS c FROM context_entries")
        return int(row["c"]) if row else 0

    async def applicable(
        self,
        *,
        guild_id: Optional[int],
        channel_id: Optional[int],
        user_id: Optional[int],
    ) -> list[ContextEntry]:
        """Return every entry that applies to the given location.

        Only the *specified* scopes are returned, which is what guarantees
        isolation between servers, channels and users.
        """

        rows: list[aiosqlite.Row] = []
        rows += await self.db.fetchall(
            "SELECT * FROM context_entries WHERE scope = ? ORDER BY priority DESC, id ASC",
            (Scope.ALL.value,),
        )
        if guild_id is not None:
            rows += await self.db.fetchall(
                "SELECT * FROM context_entries WHERE scope = ? AND target_id = ? ORDER BY priority DESC, id ASC",
                (Scope.SERVER.value, str(guild_id)),
            )
        if channel_id is not None:
            rows += await self.db.fetchall(
                "SELECT * FROM context_entries WHERE scope = ? AND target_id = ? ORDER BY priority DESC, id ASC",
                (Scope.CHANNEL.value, str(channel_id)),
            )
        if user_id is not None:
            rows += await self.db.fetchall(
                "SELECT * FROM context_entries WHERE scope = ? AND target_id = ? ORDER BY priority DESC, id ASC",
                (Scope.USER.value, str(user_id)),
            )
        return [_row_to_entry(r) for r in rows]


class ConversationRepository:
    """Persistence for conversations and their message history."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def build_scope_key(
        *,
        kind: str,
        guild_id: Optional[int],
        channel_id: Optional[int],
        user_id: Optional[int],
    ) -> str:
        g = guild_id if guild_id is not None else "-"
        c = channel_id if channel_id is not None else "-"
        u = user_id if user_id is not None else "-"
        return f"{kind}:g{g}:c{c}:u{u}"

    async def get_or_create(
        self,
        *,
        kind: str,
        guild_id: Optional[int],
        channel_id: Optional[int],
        user_id: Optional[int],
    ) -> Conversation:
        scope_key = self.build_scope_key(
            kind=kind, guild_id=guild_id, channel_id=channel_id, user_id=user_id
        )
        row = await self.db.fetchone("SELECT * FROM conversations WHERE scope_key = ?", (scope_key,))
        if row:
            return _row_to_conversation(row)

        now = utcnow_iso()
        await self.db.execute(
            """
            INSERT OR IGNORE INTO conversations
                (scope_key, kind, guild_id, channel_id, user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope_key,
                kind,
                str(guild_id) if guild_id is not None else None,
                str(channel_id) if channel_id is not None else None,
                str(user_id) if user_id is not None else None,
                now,
                now,
            ),
        )
        row = await self.db.fetchone("SELECT * FROM conversations WHERE scope_key = ?", (scope_key,))
        assert row is not None
        return _row_to_conversation(row)

    async def get(self, conversation_id: int) -> Optional[Conversation]:
        row = await self.db.fetchone("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return _row_to_conversation(row) if row else None

    async def add_message(
        self,
        conversation_id: int,
        *,
        role: Role,
        content: str,
        author_id: Optional[int] = None,
        author_name: Optional[str] = None,
        token_estimate: int = 0,
    ) -> int:
        now = utcnow_iso()
        message_id = await self.db.execute(
            """
            INSERT INTO messages
                (conversation_id, role, author_id, author_name, content, token_estimate, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role.value,
                str(author_id) if author_id is not None else None,
                author_name,
                content,
                token_estimate,
                now,
            ),
        )
        await self.db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
        )
        return message_id

    async def recent_messages(
        self, conversation_id: int, limit: int, *, after_id: int = 0
    ) -> list[StoredMessage]:
        """Return the newest ``limit`` messages in chronological order."""

        rows = await self.db.fetchall(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE conversation_id = ? AND id > ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (conversation_id, after_id, max(1, limit)),
        )
        return [_row_to_message(r) for r in rows]

    async def messages_since(
        self, conversation_id: int, after_id: int, limit: int = 500
    ) -> list[StoredMessage]:
        rows = await self.db.fetchall(
            "SELECT * FROM messages WHERE conversation_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (conversation_id, after_id, max(1, limit)),
        )
        return [_row_to_message(r) for r in rows]

    async def count_messages(self, conversation_id: int) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ?", (conversation_id,)
        )
        return int(row["c"]) if row else 0

    async def set_summary(self, conversation_id: int, summary: str, upto_message_id: int) -> None:
        await self.db.execute(
            "UPDATE conversations SET summary = ?, summary_upto = ?, updated_at = ? WHERE id = ?",
            (summary, upto_message_id, utcnow_iso(), conversation_id),
        )

    async def delete_conversation(self, conversation_id: int) -> bool:
        row = await self.db.fetchone("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
        if not row:
            return False
        await self.db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        await self.db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return True

    async def purge_older_than(self, cutoff_iso: str) -> int:
        """Delete conversations not updated since ``cutoff_iso`` (retention policy)."""

        rows = await self.db.fetchall(
            "SELECT id FROM conversations WHERE updated_at < ?", (cutoff_iso,)
        )
        ids = [int(r["id"]) for r in rows]
        for conversation_id in ids:
            await self.delete_conversation(conversation_id)
        return len(ids)


class FileMetadataRepository:
    """Persistence for uploaded/generated file metadata."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add_file(
        self,
        *,
        direction: str,
        filename: str,
        conversation_id: Optional[int] = None,
        mime_type: Optional[str] = None,
        size_bytes: int = 0,
        sha256: Optional[str] = None,
    ) -> int:
        return await self.db.execute(
            """
            INSERT INTO file_metadata
                (conversation_id, direction, filename, mime_type, size_bytes, sha256, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                direction,
                filename,
                mime_type,
                size_bytes,
                sha256,
                utcnow_iso(),
            ),
        )

    async def list_files(
        self, conversation_id: Optional[int], *, limit: int = 50
    ) -> list[FileRecord]:
        if conversation_id is None:
            rows = await self.db.fetchall(
                "SELECT * FROM file_metadata ORDER BY id DESC LIMIT ?", (max(1, limit),)
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM file_metadata WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (conversation_id, max(1, limit)),
            )
        return [
            FileRecord(
                id=r["id"],
                conversation_id=r["conversation_id"],
                direction=r["direction"],
                filename=r["filename"],
                mime_type=r["mime_type"],
                size_bytes=r["size_bytes"],
                sha256=r["sha256"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
