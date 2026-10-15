"""Persistence for permission grants."""

from __future__ import annotations

from typing import Optional

import aiosqlite

from database.database import Database
from permissions.models import Grant, PermissionScope, ResourceType
from utils import utcnow_iso


def _row_to_grant(row: aiosqlite.Row) -> Grant:
    return Grant(
        id=row["id"],
        principal_id=int(row["principal_id"]),
        scope=PermissionScope(row["scope"]),
        resource_type=ResourceType(row["resource_type"]),
        resource_id=row["resource_id"],
        auto_prod=bool(row["auto_prod"]),
        created_by=int(row["created_by"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


class PermissionStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(
        self,
        *,
        principal_id: int,
        scope: PermissionScope,
        resource_type: ResourceType,
        resource_id: str,
        created_by: int,
        auto_prod: bool = False,
        expires_at: Optional[str] = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO permission_grants
                (principal_id, scope, resource_type, resource_id, auto_prod, created_by, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (principal_id, scope, resource_type, resource_id) DO UPDATE SET
                auto_prod = excluded.auto_prod,
                created_by = excluded.created_by,
                expires_at = excluded.expires_at
            """,
            (
                str(principal_id),
                scope.value,
                resource_type.value,
                resource_id,
                1 if auto_prod else 0,
                str(created_by),
                utcnow_iso(),
                expires_at,
            ),
        )

    async def delete(
        self,
        *,
        principal_id: int,
        scope: PermissionScope,
        resource_type: ResourceType,
        resource_id: str,
    ) -> bool:
        row = await self.db.fetchone(
            "SELECT id FROM permission_grants WHERE principal_id = ? AND scope = ? "
            "AND resource_type = ? AND resource_id = ?",
            (str(principal_id), scope.value, resource_type.value, resource_id),
        )
        if not row:
            return False
        await self.db.execute(
            "DELETE FROM permission_grants WHERE principal_id = ? AND scope = ? "
            "AND resource_type = ? AND resource_id = ?",
            (str(principal_id), scope.value, resource_type.value, resource_id),
        )
        return True

    async def list(self, principal_id: Optional[int] = None) -> list[Grant]:
        if principal_id is None:
            rows = await self.db.fetchall("SELECT * FROM permission_grants ORDER BY id ASC")
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM permission_grants WHERE principal_id = ? ORDER BY id ASC",
                (str(principal_id),),
            )
        return [_row_to_grant(r) for r in rows]

    async def for_scope(self, principal_id: int, scope: PermissionScope) -> list[Grant]:
        rows = await self.db.fetchall(
            "SELECT * FROM permission_grants WHERE principal_id = ? AND scope = ?",
            (str(principal_id), scope.value),
        )
        return [_row_to_grant(r) for r in rows]
