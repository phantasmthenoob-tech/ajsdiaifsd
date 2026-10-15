"""Persistence layer: schema, migrations and a thin async SQLite wrapper."""

from database.database import Database, DatabaseError, parse_sqlite_path
from database.schema import SCHEMA_VERSION, MIGRATIONS

__all__ = [
    "Database",
    "DatabaseError",
    "parse_sqlite_path",
    "SCHEMA_VERSION",
    "MIGRATIONS",
]
