"""Database schema and migrations.

Migrations are *forward-only* and applied in order.  Each entry is a
``(version, sql)`` pair where the SQL is executed with ``executescript``.  The
repository layer only ever talks to these tables, which keeps the SQLite
implementation swappable for PostgreSQL later.
"""

from __future__ import annotations

SCHEMA_VERSION = 3

MIGRATION_1_INITIAL = """
PRAGMA foreign_keys = ON;

-- Owner-managed context entries. `scope` + `target_id` decide where an entry
-- applies. `target_id` is NULL for global (scope='all') entries and holds the
-- numeric Discord ID of a user / channel / guild otherwise.
CREATE TABLE IF NOT EXISTS context_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT    NOT NULL CHECK (scope IN ('all', 'user', 'channel', 'server')),
    target_id   TEXT,
    role        TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT    NOT NULL,
    label       TEXT,
    priority    INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    CHECK (
        (scope = 'all' AND target_id IS NULL)
        OR (scope <> 'all' AND target_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_context_scope_target
    ON context_entries (scope, target_id);
CREATE INDEX IF NOT EXISTS idx_context_label
    ON context_entries (scope, target_id, label);

-- One conversation per (guild, channel, user, kind) combination. `scope_key`
-- is a deterministic string used for the UNIQUE constraint.
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key   TEXT    NOT NULL UNIQUE,
    kind        TEXT    NOT NULL CHECK (kind IN ('channel', 'dm')),
    guild_id    TEXT,
    channel_id  TEXT,
    user_id     TEXT,
    summary     TEXT,
    summary_upto INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_lookup
    ON conversations (guild_id, channel_id, user_id);

-- Individual messages belonging to a conversation. Author identity is stored so
-- the AI can understand who said what in a shared channel.
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT    NOT NULL CHECK (role IN ('system', 'developer', 'user', 'assistant')),
    author_id       TEXT,
    author_name     TEXT,
    content         TEXT    NOT NULL,
    token_estimate  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (conversation_id, id);

-- Metadata about files that were uploaded or generated during a conversation.
CREATE TABLE IF NOT EXISTS file_metadata (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    direction       TEXT    NOT NULL CHECK (direction IN ('upload', 'generated', 'edited')),
    filename        TEXT    NOT NULL,
    mime_type       TEXT,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    sha256          TEXT,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_file_metadata_conversation
    ON file_metadata (conversation_id, id);
"""

MIGRATION_2_DEPLOY_TARGETS = """
PRAGMA foreign_keys = ON;

-- Deployment targets owned by the owner-controlled infrastructure plane.
-- `scope_key` keeps separate Discord servers' targets isolated.
CREATE TABLE IF NOT EXISTS deploy_targets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key   TEXT    NOT NULL,
    provider    TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    external_id TEXT    NOT NULL,
    url         TEXT,
    status      TEXT    NOT NULL DEFAULT 'unknown',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    UNIQUE (scope_key, name)
);

CREATE INDEX IF NOT EXISTS idx_deploy_targets_scope
    ON deploy_targets (scope_key, name);

-- Which named target a scope currently deploys to (the "/vm swap" pointer).
CREATE TABLE IF NOT EXISTS active_targets (
    scope_key   TEXT PRIMARY KEY,
    target_name TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

MIGRATION_3_AGENT = """
PRAGMA foreign_keys = ON;

-- Explicit, owner-granted permissions. Permissions are NEVER inferred from a
-- model plan or from stored context; they are read from this table only.
CREATE TABLE IF NOT EXISTS permission_grants (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_id  TEXT    NOT NULL,
    scope         TEXT    NOT NULL,
    resource_type TEXT    NOT NULL CHECK (resource_type IN ('global','repo','vm','service','domain')),
    resource_id   TEXT    NOT NULL DEFAULT '*',
    auto_prod     INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    expires_at    TEXT,
    UNIQUE (principal_id, scope, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_permission_grants_lookup
    ON permission_grants (principal_id, scope, resource_type, resource_id);

-- Approval requests for destructive/privileged operations. The exact operation
-- and target are captured, and only the configured owner may decide.
CREATE TABLE IF NOT EXISTS approval_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     TEXT    NOT NULL,
    operation    TEXT    NOT NULL,
    target       TEXT    NOT NULL,
    reason       TEXT,
    requested_by TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','denied','expired','consumed')),
    task_id      INTEGER,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    decided_at   TEXT,
    decided_by   TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approval_requests (status, id);

-- Durable task state, so a restart never loses a job or falsely reports success.
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id   TEXT    NOT NULL,
    guild_id       TEXT,
    channel_id     TEXT,
    prompt         TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    plan_summary   TEXT,
    tool_calls     INTEGER NOT NULL DEFAULT 0,
    max_tool_calls INTEGER NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL,
    deadline       TEXT,
    result         TEXT,
    error          TEXT,
    approval_id    INTEGER,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status, id);

CREATE TABLE IF NOT EXISTS task_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_index  INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    tool        TEXT,
    args        TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    result      TEXT,
    error       TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps (task_id, step_index);

-- Registered infrastructure the agent is allowed to touch (VMs, domains, services).
CREATE TABLE IF NOT EXISTS trusted_targets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key  TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    config     TEXT,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    UNIQUE (scope_key, kind, name)
);

CREATE INDEX IF NOT EXISTS idx_trusted_targets_scope ON trusted_targets (scope_key, kind);
"""

# Ordered list of (version, sql). Append new migrations; never edit in place.
MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1_INITIAL),
    (2, MIGRATION_2_DEPLOY_TARGETS),
    (3, MIGRATION_3_AGENT),
]
