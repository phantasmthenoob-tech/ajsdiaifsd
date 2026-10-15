"""Activation tests: the bot must only respond when explicitly summoned."""

from __future__ import annotations

import types

from discord_client import AIChatCog, strip_bot_mentions
from utils import chunk_text

BOT_ID = 555555555555555555


class FakeAuthor:
    def __init__(self, id: int, *, bot: bool = False, name: str = "user", display_name: str = "User"):
        self.id = id
        self.bot = bot
        self.name = name
        self.display_name = display_name


class FakeMessage:
    def __init__(self, content, author, *, mentions=(), reference=None, guild=None, channel=None):
        self.content = content
        self.author = author
        self.mentions = list(mentions)
        self.reference = reference
        self.guild = guild
        self.channel = channel
        self.attachments = []


class SpyEngine:
    def __init__(self):
        self.calls = 0

    async def handle(self, request):
        self.calls += 1
        raise AssertionError("engine must not be called for un-summoned messages")


def make_cog(config, engine=None):
    chatbot = types.SimpleNamespace(
        config=config,
        engine=engine or SpyEngine(),
        context_service=None,
        bot=types.SimpleNamespace(user=types.SimpleNamespace(id=BOT_ID)),
    )
    cog = AIChatCog(chatbot)
    return cog, chatbot


def test_ordinary_message_is_ignored(config):
    cog, _ = make_cog(config)
    message = FakeMessage("hello everyone", FakeAuthor(1))
    summoned, _ = cog._summoned(message, BOT_ID)
    assert summoned is False


def test_direct_mention_summons_and_strips(config):
    cog, _ = make_cog(config)
    mention = FakeAuthor(BOT_ID, bot=True, name="bot")
    message = FakeMessage(f"<@{BOT_ID}> hey, what's up?", FakeAuthor(1), mentions=[mention])
    summoned, prompt = cog._summoned(message, BOT_ID)
    assert summoned is True
    assert prompt == "hey, what's up?"
    assert f"<@{BOT_ID}>" not in prompt


def test_indirect_mention_ignored_when_strict(config):
    cog, _ = make_cog(config)
    mention = FakeAuthor(BOT_ID, bot=True, name="bot")
    message = FakeMessage("hey bot, I was talking to someone", FakeAuthor(1), mentions=[mention])
    summoned, _ = cog._summoned(message, BOT_ID)
    assert summoned is False


def test_indirect_mention_allowed_when_not_strict(config):
    not_strict = types.SimpleNamespace(
        conversation=types.SimpleNamespace(strict_mention=False, enable_reply_conversations=True)
    )
    cog, _ = make_cog(not_strict)
    mention = FakeAuthor(BOT_ID, bot=True, name="bot")
    message = FakeMessage("could you help me with this?", FakeAuthor(1), mentions=[mention])
    summoned, prompt = cog._summoned(message, BOT_ID)
    assert summoned is True
    assert prompt == "could you help me with this?"


def test_strip_bot_mentions_handles_both_forms():
    assert strip_bot_mentions(f"<@{BOT_ID}> hi", BOT_ID, strict=True).strip() == "hi"
    assert strip_bot_mentions(f"<@!{BOT_ID}> hi", BOT_ID, strict=True).strip() == "hi"


async def test_bot_authored_messages_are_ignored(config):
    engine = SpyEngine()
    cog, _ = make_cog(config, engine)
    message = FakeMessage("beep boop", FakeAuthor(2, bot=True))
    await cog.on_message(message)  # must return early without error
    assert engine.calls == 0


async def test_un_summoned_human_message_is_ignored(config):
    engine = SpyEngine()
    cog, _ = make_cog(config, engine)
    message = FakeMessage("just chatting", FakeAuthor(1))
    await cog.on_message(message)
    assert engine.calls == 0


def test_chunk_text_respects_limit_and_preserves_content():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = chunk_text(text, 200)
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    # A single very long line is hard-split rather than dropped.
    assert all(len(chunk) <= 50 for chunk in chunk_text("a" * 500, 50))
