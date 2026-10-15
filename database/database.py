"""A thin, async SQLite wrapper built on :mod:`aiosqlite`.

The wrapper deliberately exposes only a small surface (``execute``, ``fetchone``,
``fetchall``, ``executescript`` and ``transaction``) so the repositories above it
do not depend on aiosqlite directly.  A future PostgreSQL implementation can
satisfy the same surface.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable, Optional, Sequence

import aiosqlite

from database.schema import MIGRATIONS, SCHEMA_VERSION


class DatabaseError(RuntimeError):
    """Raised for database-level (non-SQL) failures."""


def parse_sqlite_path(url: str) -> str:
    """Translate a ``DATABASE_URL`` into a filesystem path or ``:memory:``."""

    if not url:
        raise DatabaseError("DATABASE_URL is empty")
    url = url.strip()
    if url in {":memory:", "sqlite://:memory:", "sqlite:///:memory:"}:
        return ":memory:"
    lowered = url.lower()
    if "://" in url:
        scheme = url.split("://", 1)[0].lower()
        if scheme not in {"sqlite", "sqlite3"}:
            raise DatabaseError(
                f"Unsupported database scheme {scheme!r}. Only SQLite is implemented in "
                "this release; the repository interface is designed to be swapped for "
                "PostgreSQL by providing a new Database implementation."
            )
    for prefix in ("sqlite:///", "sqlite3:///", "sqlite://", "sqlite3://", "sqlite:", "sqlite3:"):
        if lowered.startswith(prefix):
            return url[len(prefix):]
    return url


class Database:
    """Owns a single :class:`aiosqlite.Connection`."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.path = parse_sqlite_path(url)
        self._conn: Optional[aiosqlite.Connection] = None
        self._tx_lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------
    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("Database is not connected; call await db.connect() first")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if self.path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.commit()

    async def initialize(self) -> int:
        """Apply pending migrations and return the resulting schema version."""

        await self.connect()
        conn = self.connection
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await conn.commit()

        async with conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations") as cur:
            row = await cur.fetchone()
        current = int(row["v"]) if row else 0

        for version, sql in sorted(MIGRATIONS, key=lambda m: m[0]):
            if version <= current:
                continue
            await conn.executescript(sql)
            await conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                (version,),
            )
            await conn.commit()
            current = version

        return current

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        await self.initialize()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # -- queries ------------------------------------------------------------
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        conn = self.connection
        cur = await conn.execute(sql, tuple(params))
        await conn.commit()
        lastrowid = cur.lastrowid or 0
        await cur.close()
        return int(lastrowid)

    async def executemany(self, sql: str, seq_params: Iterable[Sequence[Any]]) -> None:
        conn = self.connection
        await conn.executemany(sql, [tuple(p) for p in seq_params])
        await conn.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[aiosqlite.Row]:
        conn = self.connection
        async with conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        conn = self.connection
        async with conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialise write transactions. SQLite has a single writer."""

        conn = self.connection
        async with self._tx_lock:
            await conn.execute("BEGIN")
            try:
                yield conn
            except Exception:
                await conn.rollback()
                raise
            else:
                await conn.commit()
