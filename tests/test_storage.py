"""SQLite persistence, search and memory."""

from __future__ import annotations

from pyassist.core.models import Message, ToolCall


async def test_create_and_list_conversations(storage) -> None:
    first = await storage.create_conversation(title="First")
    await storage.create_conversation(title="Second")

    conversations = await storage.list_conversations()
    assert len(conversations) == 2
    # Most recently updated first.
    assert conversations[0].title == "Second"
    assert await storage.get_conversation(first.id) is not None


async def test_messages_round_trip(storage) -> None:
    convo = await storage.create_conversation()
    await storage.add_message(Message(role="user", content="hi", conversation_id=convo.id))
    await storage.add_message(
        Message(role="assistant", content="hello", conversation_id=convo.id)
    )

    messages = await storage.get_messages(convo.id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == "hello"


async def test_tool_calls_survive_round_trip(storage) -> None:
    """Tool calls are stored as JSON; they must deserialise intact."""
    convo = await storage.create_conversation()
    call = ToolCall(id="call_1", name="read_file", arguments='{"path": "a.txt"}')
    await storage.add_message(
        Message(role="assistant", content="", conversation_id=convo.id, tool_calls=[call])
    )

    messages = await storage.get_messages(convo.id)
    restored = messages[0].tool_calls[0]
    assert restored.id == "call_1"
    assert restored.name == "read_file"
    assert restored.parsed_arguments() == {"path": "a.txt"}


async def test_delete_conversation_removes_messages(storage) -> None:
    convo = await storage.create_conversation()
    await storage.add_message(Message(role="user", content="x", conversation_id=convo.id))

    await storage.delete_conversation(convo.id)
    assert await storage.get_conversation(convo.id) is None
    assert await storage.get_messages(convo.id) == []


async def test_delete_messages_after(storage) -> None:
    convo = await storage.create_conversation()
    first = Message(role="user", content="keep", conversation_id=convo.id)
    await storage.add_message(first)
    later = Message(role="assistant", content="drop", conversation_id=convo.id)
    later.created_at = first.created_at + 10
    await storage.add_message(later)

    await storage.delete_messages_after(convo.id, first.created_at)
    remaining = await storage.get_messages(convo.id)
    assert [m.content for m in remaining] == ["keep"]


async def test_search_finds_messages(storage) -> None:
    convo = await storage.create_conversation(title="Databases")
    await storage.add_message(
        Message(role="user", content="explain sqlite WAL mode", conversation_id=convo.id)
    )

    results = await storage.search_messages("sqlite")
    assert results
    found_convo, found_message = results[0]
    assert found_convo.id == convo.id
    assert "sqlite" in found_message.content


async def test_search_handles_fts_special_characters(storage) -> None:
    """FTS5 treats quotes and operators specially; queries must not crash."""
    convo = await storage.create_conversation()
    await storage.add_message(
        Message(role="user", content="a quoted phrase", conversation_id=convo.id)
    )

    for query in ['"unclosed', "AND", "*", "NEAR(", "foo OR"]:
        assert isinstance(await storage.search_messages(query), list)


async def test_memory_upsert_and_search(storage) -> None:
    await storage.upsert_memory("timezone", "UTC+5:30")
    await storage.upsert_memory("editor", "neovim")
    # Upserting the same key replaces rather than duplicates.
    await storage.upsert_memory("timezone", "UTC+1")

    memories = await storage.list_memories()
    assert len(memories) == 2
    assert {m.key for m in memories} == {"timezone", "editor"}
    assert next(m.value for m in memories if m.key == "timezone") == "UTC+1"

    found = await storage.search_memories("editor")
    assert found and found[0].value == "neovim"


async def test_delete_memory(storage) -> None:
    await storage.upsert_memory("k", "v")
    assert await storage.delete_memory("k") is True
    assert await storage.delete_memory("missing") is False


async def test_rename_and_touch(storage) -> None:
    convo = await storage.create_conversation()
    await storage.rename_conversation(convo.id, "Renamed")
    await storage.touch_conversation(convo.id, model="gpt-4o")

    reloaded = await storage.get_conversation(convo.id)
    assert reloaded.title == "Renamed"
    assert reloaded.model == "gpt-4o"


async def test_stats(storage) -> None:
    convo = await storage.create_conversation()
    await storage.add_message(Message(role="user", content="x", conversation_id=convo.id))

    stats = await storage.stats()
    assert stats["conversations"] == 1
    assert stats["messages"] == 1
