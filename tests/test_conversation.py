"""Conversation assembly, priority and token-budget tests."""

from __future__ import annotations

from ai.conversation import (
    SYSTEM_DEVELOPER,
    SYSTEM_SAFETY,
    UNTRUSTED_PREAMBLE,
    Attachment,
    build_messages,
    estimate_tokens,
)
from context.models import ContextEntry, Role, Scope, StoredMessage


def entry(scope: Scope, role: Role, content: str) -> ContextEntry:
    return ContextEntry(scope=scope, role=role, content=content)


def stored(conv_id: int, role: Role, content: str, *, mid: int, name: str = "user") -> StoredMessage:
    return StoredMessage(conversation_id=conv_id, role=role, content=content, author_id=1, author_name=name, id=mid)


def test_system_prompts_are_first_and_separate():
    result = build_messages(
        context_layers={},
        history=[],
        current_prompt="hello",
        max_tokens=2000,
    )
    assert result.messages[0].role == "system"
    assert result.messages[0].content == SYSTEM_SAFETY
    assert result.messages[1].role == "system"
    assert result.messages[1].content == SYSTEM_DEVELOPER
    assert result.messages[-1].role == "user"
    assert result.messages[-1].content == "hello"


def test_context_roles_are_preserved_and_not_converted_to_system():
    layers = {
        Scope.ALL: [entry(Scope.ALL, Role.USER, "global user note"),
                    entry(Scope.ALL, Role.ASSISTANT, "global assistant note")],
        Scope.USER: [entry(Scope.USER, Role.USER, "user note")],
    }
    result = build_messages(context_layers=layers, history=[], current_prompt="hi", max_tokens=2000)
    roles = [(m.role, m.content) for m in result.messages]
    assert ("user", "global user note") in roles
    assert ("assistant", "global assistant note") in roles
    assert ("user", "user note") in roles
    # No stored entry ever became a system/developer message.
    assert ("system", "global user note") not in roles
    assert ("system", "global assistant note") not in roles
    assert ("system", "user note") not in roles


def test_priority_order_global_before_history_before_current():
    layers = {Scope.ALL: [entry(Scope.ALL, Role.USER, "GLOBAL-CONTEXT")]}
    history = [stored(1, Role.USER, "HISTORY-A", mid=1), stored(1, Role.ASSISTANT, "HISTORY-B", mid=2)]
    result = build_messages(
        context_layers=layers, history=history, current_prompt="CURRENT", max_tokens=2000
    )
    contents = [m.content for m in result.messages]
    assert contents.index("GLOBAL-CONTEXT") < contents.index("HISTORY-A")
    assert contents.index("HISTORY-B") < contents.index("CURRENT")


def test_attachments_are_framed_as_untrusted():
    result = build_messages(
        context_layers={},
        history=[],
        current_prompt="review this",
        attachments=[Attachment("evil.py", "print('x')")],
        max_tokens=2000,
    )
    assert UNTRUSTED_PREAMBLE in result.messages[-1].content
    assert "evil.py" in result.messages[-1].content


def test_history_is_trimmed_under_budget_but_current_is_kept():
    big = "word " * 400  # ~500 tokens each
    history = [stored(1, Role.USER, big, mid=i) for i in range(20)]
    result = build_messages(
        context_layers={},
        history=history,
        current_prompt="FINAL-QUESTION",
        max_tokens=800,
        reserve_output_tokens=200,
    )
    assert result.dropped_history > 0
    assert result.truncated is True
    assert result.messages[-1].content == "FINAL-QUESTION"


def test_author_identity_included_for_channels():
    history = [stored(1, Role.USER, "hello there", mid=1, name="alice")]
    result = build_messages(
        context_layers={},
        history=history,
        current_prompt="hi",
        include_author_names=True,
        max_tokens=2000,
    )
    assert any(m.content == "alice: hello there" for m in result.messages)


def test_estimate_tokens_positive():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") >= 1
