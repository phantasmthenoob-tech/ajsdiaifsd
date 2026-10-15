"""Registry of trusted infrastructure targets.

Only targets registered here (by the owner) may be addressed by agent tools and
commands. Registration also records the grant that authorizes its use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

import aiosqlite

from database.database import Database
from utils import utcnow_iso

VALID_KINDS = {"vm", "domain", "service"}


@dataclass
class TrustedTarget:
    scope_key: str
    kind: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _row_to_target(row: aiosqlite.Row) -> TrustedTarget:
    config: dict[str, Any] = {}
    if row["config"]:
        try:
            config = json.loads(row["config"])
        except json.JSONDecodeError:
            config = {}
    return TrustedTarget(
        id=row["id"],
        scope_key=row["scope_key"],
        kind=row["kind"],
        name=row["name"],
        config=config,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class TargetRegistryError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class TrustedTargetStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def register(
        self, *, scope_key: str, kind: str, name: str, config: Optional[dict] = None
    ) -> TrustedTarget:
        kind = (kind or "").strip().lower()
        name = (name or "").strip().lower()
        if kind not in VALID_KINDS:
            raise TargetRegistryError(f"`{kind}` is not a valid target kind ({', '.join(sorted(VALID_KINDS))}).")
        if not name:
            raise TargetRegistryError("A target name is required.")
        now = utcnow_iso()
        await self.db.execute(
            """
            INSERT INTO trusted_targets (scope_key, kind, name, config, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (scope_key, kind, name) DO UPDATE SET
                config = excluded.config, updated_at = excluded.updated_at
            """,
            (scope_key, kind, name, json.dumps(config or {}), now, now),
        )
        target = await self.get(scope_key=scope_key, kind=kind, name=name)
        assert target is not None
        return target

    async def unregister(self, *, scope_key: str, kind: str, name: str) -> bool:
        row = await self.db.fetchone(
            "SELECT id FROM trusted_targets WHERE scope_key = ? AND kind = ? AND name = ?",
            (scope_key, kind.strip().lower(), name.strip().lower()),
        )
        if not row:
            return False
        await self.db.execute(
            "DELETE FROM trusted_targets WHERE scope_key = ? AND kind = ? AND name = ?",
            (scope_key, kind.strip().lower(), name.strip().lower()),
        )
        return True

    async def get(self, *, scope_key: str, kind: str, name: str) -> Optional[TrustedTarget]:
        row = await self.db.fetchone(
            "SELECT * FROM trusted_targets WHERE scope_key = ? AND kind = ? AND name = ?",
            (scope_key, kind.strip().lower(), name.strip().lower()),
        )
        return _row_to_target(row) if row else None

    async def list(self, scope_key: Optional[str] = None, kind: Optional[str] = None) -> list[TrustedTarget]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope_key is not None:
            clauses.append("scope_key = ?")
            params.append(scope_key)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.db.fetchall(
            f"SELECT * FROM trusted_targets {where} ORDER BY kind, name", params
        )
        return [_row_to_target(r) for r in rows]
