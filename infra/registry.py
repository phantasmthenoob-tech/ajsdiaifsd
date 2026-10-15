"""Persistence for deploy targets and the active-target pointer.

Targets are scoped by ``scope_key`` (typically the Discord guild), so one
server's targets are never visible from another.
"""

from __future__ import annotations

from typing import Optional

import aiosqlite

from database.database import Database
from infra.models import Target
from utils import utcnow_iso


def _row_to_target(row: aiosqlite.Row) -> Target:
    return Target(
        provider=row["provider"],
        name=row["name"],
        external_id=row["external_id"],
        url=row["url"],
        status=row["status"],
        raw={"id": row["id"], "created_at": row["created_at"], "updated_at": row["updated_at"]},
    )


class TargetRegistry:
    """CRUD for tracked targets plus the per-scope active target."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(self, scope_key: str, target: Target) -> None:
        now = utcnow_iso()
        await self.db.execute(
            """
            INSERT INTO deploy_targets
                (scope_key, provider, name, external_id, url, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (scope_key, name) DO UPDATE SET
                provider = excluded.provider,
                external_id = excluded.external_id,
                url = excluded.url,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (scope_key, target.provider, target.name, target.external_id, target.url, target.status, now, now),
        )

    async def get(self, scope_key: str, name: str) -> Optional[Target]:
        row = await self.db.fetchone(
            "SELECT * FROM deploy_targets WHERE scope_key = ? AND name = ?",
            (scope_key, name.strip().lower()),
        )
        return _row_to_target(row) if row else None

    async def list(self, scope_key: str) -> list[Target]:
        rows = await self.db.fetchall(
            "SELECT * FROM deploy_targets WHERE scope_key = ? ORDER BY name ASC", (scope_key,)
        )
        return [_row_to_target(r) for r in rows]

    async def count(self, scope_key: str) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS c FROM deploy_targets WHERE scope_key = ?", (scope_key,)
        )
        return int(row["c"]) if row else 0

    async def delete(self, scope_key: str, name: str) -> bool:
        name = name.strip().lower()
        row = await self.db.fetchone(
            "SELECT id FROM deploy_targets WHERE scope_key = ? AND name = ?", (scope_key, name)
        )
        if not row:
            return False
        await self.db.execute(
            "DELETE FROM deploy_targets WHERE scope_key = ? AND name = ?", (scope_key, name)
        )
        await self.db.execute(
            "DELETE FROM active_targets WHERE scope_key = ? AND target_name = ?", (scope_key, name)
        )
        return True

    # -- active target pointer ---------------------------------------------
    async def set_active(self, scope_key: str, name: str) -> None:
        await self.db.execute(
            """
            INSERT INTO active_targets (scope_key, target_name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (scope_key) DO UPDATE SET
                target_name = excluded.target_name, updated_at = excluded.updated_at
            """,
            (scope_key, name.strip().lower(), utcnow_iso()),
        )

    async def get_active_name(self, scope_key: str) -> Optional[str]:
        row = await self.db.fetchone(
            "SELECT target_name FROM active_targets WHERE scope_key = ?", (scope_key,)
        )
        return row["target_name"] if row else None

    async def get_active(self, scope_key: str) -> Optional[Target]:
        name = await self.get_active_name(scope_key)
        if not name:
            return None
        return await self.get(scope_key, name)
