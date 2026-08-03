"""Async SQLite persistence for conversations, messages and memories.

Why SQLite (and this shape)
---------------------------
* Zero-config, single file, already in the stdlib - ideal for a local TUI.
* WAL journal + ``synchronous=NORMAL`` keeps the UI responsive while writing.
* A schema version table lets us migrate forward without dropping user data.
* An optional FTS5 virtual table powers ``/search``. FTS5 is compiled into most
  Python builds but not all, so it degrades gracefully to ``LIKE``.

All public methods are coroutines and safe to call from the Textual event loop.
A single connection is reused and guarded by a lock, since SQLite writes
serialise anyway and this avoids "database is locked" churn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Attachment, Conversation, Memory, Message, ToolCall, new_id, now

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT 'New chat',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    model         TEXT NOT NULL DEFAULT '',
    metadata      TEXT NOT NULL DEFAULT '{}',
    archived      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations (updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    tool_calls      TEXT NOT NULL DEFAULT '[]',
    tool_call_id    TEXT,
    tool_name       TEXT,
    attachments     TEXT NOT NULL DEFAULT '[]',
    reasoning       TEXT NOT NULL DEFAULT '',
    metadata        TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS memories (
    id                     TEXT PRIMARY KEY,
    key                    TEXT NOT NULL UNIQUE,
    value                  TEXT NOT NULL,
    created_at             REAL NOT NULL,
    updated_at             REAL NOT NULL,
    source_conversation_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memories_updated
    ON memories (updated_at DESC);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5 (
    content,
    message_id UNINDEXED,
    conversation_id UNINDEXED,
    tokenize = 'unicode61'
);
"""


class Storage:
    """Repository over a single SQLite database file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._fts_enabled = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def fts_enabled(self) -> bool:
        return self._fts_enabled

    async def connect(self) -> None:
        """Open the connection, apply pragmas and run migrations."""
        if self._db is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path)
        db.row_factory = aiosqlite.Row

        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")

        await db.executescript(_SCHEMA)

        # FTS5 is optional; fall back to LIKE search when unavailable.
        try:
            await db.executescript(_FTS_SCHEMA)
            self._fts_enabled = True
        except aiosqlite.Error:  # pragma: no cover - depends on the SQLite build
            log.warning("FTS5 unavailable; falling back to LIKE search")
            self._fts_enabled = False

        await self._migrate(db)
        await db.commit()
        self._db = db

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        """Record/upgrade the schema version. Future versions branch here."""
        async with db.execute("SELECT version FROM schema_info WHERE id = 1") as cur:
            row = await cur.fetchone()

        if row is None:
            await db.execute(
                "INSERT INTO schema_info (id, version) VALUES (1, ?)", (SCHEMA_VERSION,)
            )
            return

        version = int(row["version"])
        if version > SCHEMA_VERSION:
            log.warning(
                "Database schema v%s is newer than supported v%s", version, SCHEMA_VERSION
            )
        elif version < SCHEMA_VERSION:
            # Migrations would run in sequence here; v1 is the baseline.
            await db.execute(
                "UPDATE schema_info SET version = ? WHERE id = 1", (SCHEMA_VERSION,)
            )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Storage:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.connect() must be awaited before use")
        return self._db

    # ------------------------------------------------------------------
    # conversations
    # ------------------------------------------------------------------
    async def create_conversation(
        self, title: str = "New chat", *, model: str = ""
    ) -> Conversation:
        convo = Conversation(title=title, model=model)
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO conversations (id, title, created_at, updated_at, model, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    convo.id,
                    convo.title,
                    convo.created_at,
                    convo.updated_at,
                    convo.model,
                    json.dumps(convo.metadata),
                ),
            )
            await self._conn.commit()
        return convo

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        sql = """
            SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)
                   AS message_count
            FROM conversations c
            WHERE c.id = ?
        """
        async with self._conn.execute(sql, (conversation_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_conversation(row) if row else None

    async def list_conversations(
        self, *, limit: int = 100, include_archived: bool = False
    ) -> list[Conversation]:
        sql = """
            SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)
                   AS message_count
            FROM conversations c
            {where}
            ORDER BY c.updated_at DESC
            LIMIT ?
        """.format(where="" if include_archived else "WHERE c.archived = 0")
        async with self._conn.execute(sql, (limit,)) as cur:
            rows = await cur.fetchall()
        return [_row_to_conversation(row) for row in rows]

    async def rename_conversation(self, conversation_id: str, title: str) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now(), conversation_id),
            )
            await self._conn.commit()

    async def touch_conversation(
        self, conversation_id: str, *, model: str | None = None
    ) -> None:
        """Bump ``updated_at`` so the conversation sorts to the top."""
        async with self._lock:
            if model:
                await self._conn.execute(
                    "UPDATE conversations SET updated_at = ?, model = ? WHERE id = ?",
                    (now(), model, conversation_id),
                )
            else:
                await self._conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now(), conversation_id),
                )
            await self._conn.commit()

    async def delete_conversation(self, conversation_id: str) -> None:
        async with self._lock:
            if self._fts_enabled:
                await self._conn.execute(
                    "DELETE FROM messages_fts WHERE conversation_id = ?", (conversation_id,)
                )
            # ON DELETE CASCADE removes the messages.
            await self._conn.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
            await self._conn.commit()

    async def set_archived(self, conversation_id: str, archived: bool) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE conversations SET archived = ? WHERE id = ?",
                (1 if archived else 0, conversation_id),
            )
            await self._conn.commit()

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------
    async def add_message(self, message: Message) -> Message:
        """Insert a message and bump its conversation's ``updated_at``."""
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, role, content, created_at,
                    tool_calls, tool_call_id, tool_name, attachments, reasoning, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    message.conversation_id,
                    message.role,
                    message.content,
                    message.created_at,
                    json.dumps([tc.to_dict() for tc in message.tool_calls]),
                    message.tool_call_id,
                    message.tool_name,
                    json.dumps([a.to_dict() for a in message.attachments]),
                    message.reasoning,
                    json.dumps(message.metadata),
                ),
            )
            if self._fts_enabled and message.content and message.role in ("user", "assistant"):
                await self._conn.execute(
                    """
                    INSERT INTO messages_fts (content, message_id, conversation_id)
                    VALUES (?, ?, ?)
                    """,
                    (message.content, message.id, message.conversation_id),
                )
            await self._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (message.created_at, message.conversation_id),
            )
            await self._conn.commit()
        return message

    async def update_message(self, message: Message) -> None:
        """Persist edits to an existing message (used to finalise streams)."""
        async with self._lock:
            await self._conn.execute(
                """
                UPDATE messages
                SET content = ?, tool_calls = ?, reasoning = ?, metadata = ?
                WHERE id = ?
                """,
                (
                    message.content,
                    json.dumps([tc.to_dict() for tc in message.tool_calls]),
                    message.reasoning,
                    json.dumps(message.metadata),
                    message.id,
                ),
            )
            if self._fts_enabled:
                await self._conn.execute(
                    "DELETE FROM messages_fts WHERE message_id = ?", (message.id,)
                )
                if message.content and message.role in ("user", "assistant"):
                    await self._conn.execute(
                        """
                        INSERT INTO messages_fts (content, message_id, conversation_id)
                        VALUES (?, ?, ?)
                        """,
                        (message.content, message.id, message.conversation_id),
                    )
            await self._conn.commit()

    async def get_messages(
        self, conversation_id: str, *, limit: int | None = None
    ) -> list[Message]:
        """Return messages oldest-first. ``limit`` keeps the *newest* N."""
        if limit:
            sql = """
                SELECT * FROM (
                    SELECT * FROM messages WHERE conversation_id = ?
                    ORDER BY created_at DESC LIMIT ?
                ) ORDER BY created_at ASC
            """
            params: tuple[Any, ...] = (conversation_id, limit)
        else:
            sql = """
                SELECT * FROM messages WHERE conversation_id = ?
                ORDER BY created_at ASC
            """
            params = (conversation_id,)
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_message(row) for row in rows]

    async def delete_messages_after(self, conversation_id: str, created_at: float) -> None:
        """Drop messages newer than ``created_at`` - used by regenerate/edit."""
        async with self._lock:
            if self._fts_enabled:
                await self._conn.execute(
                    """
                    DELETE FROM messages_fts WHERE message_id IN (
                        SELECT id FROM messages
                        WHERE conversation_id = ? AND created_at > ?
                    )
                    """,
                    (conversation_id, created_at),
                )
            await self._conn.execute(
                "DELETE FROM messages WHERE conversation_id = ? AND created_at > ?",
                (conversation_id, created_at),
            )
            await self._conn.commit()

    async def search_messages(
        self, query: str, *, limit: int = 50
    ) -> list[tuple[Conversation, Message]]:
        """Full-text search across all conversations."""
        query = query.strip()
        if not query:
            return []

        if self._fts_enabled:
            sql = """
                SELECT m.*, c.title AS convo_title, c.created_at AS convo_created,
                       c.updated_at AS convo_updated, c.model AS convo_model
                FROM messages_fts f
                JOIN messages m ON m.id = f.message_id
                JOIN conversations c ON c.id = m.conversation_id
                WHERE messages_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            params: tuple[Any, ...] = (_fts_query(query), limit)
        else:  # pragma: no cover - only on builds without FTS5
            sql = """
                SELECT m.*, c.title AS convo_title, c.created_at AS convo_created,
                       c.updated_at AS convo_updated, c.model AS convo_model
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE m.content LIKE ?
                ORDER BY m.created_at DESC
                LIMIT ?
            """
            params = (f"%{query}%", limit)

        try:
            async with self._conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        except aiosqlite.Error as exc:
            # A malformed FTS expression is a user error, not a crash.
            log.debug("search failed: %s", exc)
            return []

        results: list[tuple[Conversation, Message]] = []
        for row in rows:
            convo = Conversation(
                id=row["conversation_id"],
                title=row["convo_title"],
                created_at=row["convo_created"],
                updated_at=row["convo_updated"],
                model=row["convo_model"],
            )
            results.append((convo, _row_to_message(row)))
        return results

    # ------------------------------------------------------------------
    # memories
    # ------------------------------------------------------------------
    async def upsert_memory(
        self, key: str, value: str, *, conversation_id: str = ""
    ) -> Memory:
        key = key.strip()
        timestamp = now()
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO memories (id, key, value, created_at, updated_at,
                                      source_conversation_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    source_conversation_id = excluded.source_conversation_id
                """,
                (new_id(), key, value, timestamp, timestamp, conversation_id),
            )
            await self._conn.commit()
        stored = await self.get_memory(key)
        # The row always exists after an upsert; the fallback keeps mypy happy.
        return stored or Memory(key=key, value=value)

    async def get_memory(self, key: str) -> Memory | None:
        async with self._conn.execute(
            "SELECT * FROM memories WHERE key = ?", (key.strip(),)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_memory(row) if row else None

    async def list_memories(self, *, limit: int = 200) -> list[Memory]:
        async with self._conn.execute(
            "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_memory(row) for row in rows]

    async def search_memories(self, query: str, *, limit: int = 20) -> list[Memory]:
        pattern = f"%{query.strip()}%"
        async with self._conn.execute(
            """
            SELECT * FROM memories
            WHERE key LIKE ? OR value LIKE ?
            ORDER BY updated_at DESC LIMIT ?
            """,
            (pattern, pattern, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_memory(row) for row in rows]

    async def delete_memory(self, key: str) -> bool:
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM memories WHERE key = ?", (key.strip(),)
            )
            await self._conn.commit()
            return cur.rowcount > 0

    async def clear_memories(self) -> int:
        async with self._lock:
            cur = await self._conn.execute("DELETE FROM memories")
            await self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    async def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, sql in (
            ("conversations", "SELECT COUNT(*) FROM conversations"),
            ("messages", "SELECT COUNT(*) FROM messages"),
            ("memories", "SELECT COUNT(*) FROM memories"),
        ):
            async with self._conn.execute(sql) as cur:
                row = await cur.fetchone()
            out[name] = int(row[0]) if row else 0
        out["size_bytes"] = self._path.stat().st_size if self._path.exists() else 0
        return out

    async def vacuum(self) -> None:
        async with self._lock:
            await self._conn.execute("VACUUM")
            await self._conn.commit()


# ----------------------------------------------------------------------
# row mapping helpers
# ----------------------------------------------------------------------
def _json_load(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_conversation(row: aiosqlite.Row) -> Conversation:
    keys = row.keys()
    return Conversation(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        model=row["model"],
        message_count=int(row["message_count"]) if "message_count" in keys else 0,
        metadata=_json_load(row["metadata"], {}),
    )


def _row_to_message(row: aiosqlite.Row) -> Message:
    tool_calls = [ToolCall.from_dict(d) for d in _json_load(row["tool_calls"], [])]
    attachments = [Attachment(**d) for d in _json_load(row["attachments"], [])]
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        created_at=row["created_at"],
        tool_calls=tool_calls,
        tool_call_id=row["tool_call_id"],
        tool_name=row["tool_name"],
        attachments=attachments,
        reasoning=row["reasoning"],
        metadata=_json_load(row["metadata"], {}),
    )


def _row_to_memory(row: aiosqlite.Row) -> Memory:
    return Memory(
        id=row["id"],
        key=row["key"],
        value=row["value"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source_conversation_id=row["source_conversation_id"],
    )


def _fts_query(query: str) -> str:
    """Turn user input into a safe FTS5 expression.

    Each bare word becomes a prefix term joined with AND. Quoting every token
    means FTS operators typed by the user can't produce a syntax error.
    """
    tokens = [t for t in query.replace('"', " ").split() if t]
    if not tokens:
        return '""'
    return " AND ".join(f'"{token}"*' for token in tokens)
