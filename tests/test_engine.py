"""End-to-end engine tests (no Discord, no network)."""

from __future__ import annotations

import io
import zipfile

from ai.provider import AIRateLimitError
from engine import AttachmentPayload, IncomingRequest

from tests.conftest import FailingProvider, ScriptedProvider

PATCH_RESPONSE = (
    "Fixed the sign bug.\n\n"
    "### PATCH: main.py\n"
    "<<<<<<< ORIGINAL\n    return a-b\n"
    "=======\n    return a+b\n"
    ">>>>>>> UPDATED\n"
)


def request(prompt: str, attachments=None) -> IncomingRequest:
    return IncomingRequest(
        user_id=1,
        user_name="tester",
        prompt=prompt,
        guild_id=10,
        channel_id=20,
        attachments=attachments or [],
    )


async def test_generates_python_file_as_attachment(engine):
    engine.provider = ScriptedProvider(
        ["Here is your script.\n\n### FILE: hello.py\n```python\nprint('hi')\n```\n"]
    )
    response = await engine.handle(request("write a python script"))
    assert len(response.files) == 1
    assert response.files[0].filename == "hello.py"
    assert b"print('hi')" in response.files[0].data
    # The raw file block must not be dumped into chat.
    assert "```" not in response.text


async def test_multiple_files_become_a_zip(engine):
    engine.provider = ScriptedProvider(
        [
            "### FILE: a.py\n```python\na = 1\n```\n"
            "### FILE: src/b.py\n```python\nb = 2\n```\n"
        ]
    )
    response = await engine.handle(request("create a project"))
    assert len(response.files) == 1
    assert response.files[0].filename.endswith(".zip")
    names = zipfile.ZipFile(io.BytesIO(response.files[0].data)).namelist()
    assert "a.py" in names and "src/b.py" in names


async def test_edits_uploaded_file_without_overwriting(engine):
    upload = AttachmentPayload("main.py", b"def add(a,b):\n    return a-b\n")
    engine.provider = ScriptedProvider([PATCH_RESPONSE])
    response = await engine.handle(request("fix the bug in this file", [upload]))
    assert len(response.files) == 1
    assert response.files[0].filename == "main_edited.py"
    assert b"return a+b" in response.files[0].data


async def test_unsupported_attachment_is_reported_not_swallowed(engine):
    binary = AttachmentPayload("image.png", b"\x89PNG\r\n\x1a\n\x00\x00binary")
    engine.provider = ScriptedProvider(["I could not read that."])
    response = await engine.handle(request("look at this", [binary]))
    assert response.files == []
    assert any("png" in warning.lower() or "binary" in warning.lower() for warning in response.warnings)


async def test_ai_errors_do_not_crash(engine):
    engine.provider = FailingProvider(AIRateLimitError("rate limited"))
    response = await engine.handle(request("hello"))
    assert "AI provider" in response.text
    assert response.files == []


async def test_conversation_history_is_remembered(engine):
    engine.provider = ScriptedProvider(["first reply", "second reply"])
    await engine.handle(request("my name is Bob"))
    await engine.handle(request("what is my name?"))

    last_call = engine.provider.calls[-1]
    joined = "\n".join(m.content for m in last_call)
    assert "my name is Bob" in joined
    assert "first reply" in joined


async def test_zip_upload_can_be_inspected(engine):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("src/main.py", "print('x')\n")
    upload = AttachmentPayload("project.zip", buffer.getvalue())
    engine.provider = ScriptedProvider(["Here is an explanation."])
    response = await engine.handle(request("explain this project", [upload]))
    assert "explanation" in response.text.lower()
    joined = "\n".join(m.content for m in engine.provider.calls[-1])
    assert "src/main.py" in joined
