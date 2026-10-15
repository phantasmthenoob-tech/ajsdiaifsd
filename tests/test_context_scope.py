"""Context scope isolation, persistence and operation tests."""

from __future__ import annotations

import pytest

from context.models import Role, Scope
from context.repository import ContextRepository
from context.service import ContextError, ContextService
from database.database import Database


async def test_global_context_persists_across_restart(tmp_path):
    path = str(tmp_path / "bot.db")

    db = Database(path)
    await db.initialize()
    service = ContextService(ContextRepository(db))
    await service.set_context(
        scope="all", role="user", content="The server uses Python 3.13.", target=None, label="stack"
    )
    await db.close()

    db2 = Database(path)
    await db2.initialize()
    service2 = ContextService(ContextRepository(db2))
    entries = await service2.list_context(scope="all")
    assert any("Python 3.13" in entry.content for entry in entries)
    assert entries[0].label == "stack"
    await db2.close()


async def test_role_user_and_assistant_are_preserved(context_service):
    await context_service.set_context(scope="all", content="Owner note.", role="user", target=None)
    await context_service.set_context(
        scope="all", content="We previously agreed to use PostgreSQL.", role="assistant", target=None
    )
    entries = await context_service.list_context(scope="all")
    roles = {entry.role for entry in entries}
    assert Role.USER in roles and Role.ASSISTANT in roles


async def test_scope_isolation(context_service):
    await context_service.set_context(scope="all", content="global", target=None)
    await context_service.set_context(scope="user", content="user-1", target=1)
    await context_service.set_context(scope="user", content="user-2", target=2)
    await context_service.set_context(scope="channel", content="chan-5", target=5)
    await context_service.set_context(scope="server", content="srv-9", target=9)

    layers = await context_service.applicable_context(guild_id=9, channel_id=5, user_id=1)
    assert [e.content for e in layers[Scope.ALL]] == ["global"]
    assert [e.content for e in layers[Scope.USER]] == ["user-1"]
    assert [e.content for e in layers[Scope.CHANNEL]] == ["chan-5"]
    assert [e.content for e in layers[Scope.SERVER]] == ["srv-9"]

    # A different user/channel/server only sees their own applicable entries.
    other = await context_service.applicable_context(guild_id=100, channel_id=200, user_id=2)
    assert [e.content for e in other[Scope.USER]] == ["user-2"]
    assert other[Scope.CHANNEL] == []
    assert other[Scope.SERVER] == []
    assert [e.content for e in other[Scope.ALL]] == ["global"]


async def test_append_replace_remove_by_label(context_service):
    await context_service.set_context(scope="all", content="v1", label="flow", target=None)
    await context_service.set_context(
        scope="all", content="v2", label="flow", target=None, operation="append"
    )
    entries = await context_service.list_context(scope="all")
    assert len(entries) == 1 and entries[0].content == "v2"

    await context_service.set_context(
        scope="all", content="v3", label="flow", target=None, operation="replace"
    )
    entries = await context_service.list_context(scope="all")
    assert entries[0].content == "v3"

    result = await context_service.set_context(
        scope="all", operation="remove", label="flow", target=None
    )
    assert result.affected == 1
    assert await context_service.list_context(scope="all") == []


async def test_replace_without_label_replaces_bucket(context_service):
    await context_service.set_context(scope="user", content="a", target=5)
    await context_service.set_context(scope="user", content="b", target=5)
    await context_service.set_context(scope="user", content="c", target=5, operation="replace")
    entries = await context_service.list_context(scope="user", target=5)
    assert [e.content for e in entries] == ["c"]


async def test_content_validation(context_service):
    with pytest.raises(ContextError):
        await context_service.set_context(scope="all", content="   ", target=None)
    with pytest.raises(ContextError):
        await context_service.set_context(scope="all", content="x" * 7000, target=None)


async def test_export_import_roundtrip(context_service):
    await context_service.set_context(scope="all", content="global fact", target=None, label="g")
    await context_service.set_context(scope="user", content="user fact", target=7)
    payload = await context_service.export_context()
    assert payload["count"] == 2

    await context_service.clear_context(scope=Scope.ALL)
    await context_service.clear_context(scope=Scope.USER, target=7)

    imported = await context_service.import_context(payload)
    assert imported == 2
    entries = await context_service.list_context()
    assert {entry.content for entry in entries} == {"global fact", "user fact"}


async def test_import_rejects_malformed_payloads(context_service):
    with pytest.raises(ContextError):
        await context_service.import_context({"entries": "nope"})
    with pytest.raises(ContextError):
        await context_service.import_context({"entries": [{"scope": "bogus", "content": "x"}]})
    with pytest.raises(ContextError):
        await context_service.import_context({"entries": [{"scope": "all", "role": "system", "content": "x"}]})
    with pytest.raises(ContextError):
        await context_service.import_context({"entries": [{"scope": "user", "content": "x"}]})
