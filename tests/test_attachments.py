"""Unit tests for attachment processing and storage persistence."""

from __future__ import annotations

import base64
from pathlib import Path

from pyassist.core.attachments import build_attachment
from pyassist.core.models import Attachment, Message
from pyassist.core.storage import Storage


def test_attachment_to_dict_preserves_content() -> None:
    att = Attachment(
        name="notes.txt",
        mime_type="text/plain",
        content="hello world",
        kind="text",
        size_bytes=11,
        truncated=False,
    )
    serialized = att.to_dict()
    assert serialized["content"] == "hello world"
    assert serialized["name"] == "notes.txt"

    restored = Attachment(**serialized)
    assert restored.content == "hello world"
    assert restored.name == "notes.txt"


async def test_attachment_survives_sqlite_round_trip(storage: Storage) -> None:
    convo = await storage.create_conversation()
    att = Attachment(
        name="test.png",
        mime_type="image/png",
        content="data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
        kind="image",
        size_bytes=24,
    )
    msg = Message(
        role="user",
        content="Look at this image",
        conversation_id=convo.id,
        attachments=[att],
    )
    await storage.add_message(msg)

    messages = await storage.get_messages(convo.id)
    assert len(messages) == 1
    assert len(messages[0].attachments) == 1
    loaded_att = messages[0].attachments[0]
    assert loaded_att.name == "test.png"
    assert loaded_att.kind == "image"
    assert loaded_att.content == "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


def test_build_attachment_text_file(tmp_path: Path) -> None:
    sample = tmp_path / "hello.py"
    sample.write_text("print('hello')", encoding="utf-8")

    att = build_attachment(sample)
    assert att.kind == "text"
    assert att.content == "print('hello')"
    assert not att.truncated
    assert att.mime_type in ("text/x-python", "text/plain", "application/octet-stream")


def test_build_attachment_image_file(tmp_path: Path) -> None:
    img = tmp_path / "photo.png"
    dummy_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    img.write_bytes(dummy_bytes)

    att = build_attachment(img, vision_enabled=True)
    assert att.kind == "image"
    assert att.mime_type == "image/png"
    assert att.content.startswith("data:image/png;base64,")
    encoded_data = att.content.split(",", 1)[1]
    assert base64.b64decode(encoded_data) == dummy_bytes


def test_build_attachment_oversized_file(tmp_path: Path) -> None:
    large = tmp_path / "large.txt"
    large.write_text("A" * 500, encoding="utf-8")

    att = build_attachment(large, max_bytes=100)
    assert att.truncated
    assert att.content == ""


def test_message_to_api_tool_name() -> None:
    msg = Message(
        role="tool",
        content="result data",
        tool_call_id="call_123",
        tool_name="read_file",
    )
    api_dict = msg.to_api()
    assert api_dict["role"] == "tool"
    assert api_dict["tool_call_id"] == "call_123"
    assert api_dict["content"] == "result data"
    assert api_dict["name"] == "read_file"


def test_message_char_cost() -> None:
    msg = Message(role="user", content="hello")
    assert msg.char_cost() == 5

    msg_none = Message(role="user", content="")
    assert msg_none.char_cost() == 0
