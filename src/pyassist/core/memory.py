"""Long-term memory: durable key/value facts injected into the system prompt.

How it works
------------
The memory tool (and the ``remember`` slash command) write facts like
``timezone -> Asia/Kolkata`` into SQLite via :class:`Storage`. Before each
turn, :func:`build_context_block` renders the most relevant memories into a
Markdown block that the engine prepends to the system prompt, so the model can
recall things from *other* conversations without any RAG infrastructure.

Relevance matching is deliberately simple: exact key match first, then
substring match against keys and values. Good enough for a terminal assistant,
and zero external dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Memory
from .storage import Storage


@dataclass(slots=True)
class ContextBlock:
    """A rendered memory block plus the source memories (for the UI)."""

    text: str
    memories: list[Memory]


async def build_context_block(
    storage: Storage, query: str, limit: int = 12
) -> ContextBlock:
    """Render the N most relevant memories as a Markdown block.

    Matching order:
      1. exact key match (highest priority),
      2. key contains a query word,
      3. value contains a query word (lowest priority).
    Results are de-duplicated by key.
    """
    if limit <= 0:
        return ContextBlock(text="", memories=[])

    query_words = [w.lower() for w in query.split() if w]

    exact: list[Memory] = []
    key_hits: list[Memory] = []
    value_hits: list[Memory] = []
    seen: set[str] = set()

    for mem in await storage.list_memories():
        key = mem.key.lower()
        value = mem.value.lower()
        if key in seen:
            continue

        if key == query.lower():
            bucket = exact
        elif any(word in key for word in query_words):
            bucket = key_hits
        elif any(word in value for word in query_words):
            bucket = value_hits
        else:
            continue
        seen.add(key)
        bucket.append(mem)

    picked = exact[:limit] + key_hits[: max(0, limit - len(exact))]
    remaining = limit - len(picked)
    picked += value_hits[:remaining]
    picked = picked[:limit]

    if not picked:
        return ContextBlock(text="", memories=[])

    lines = ["The following are facts the user told you about themselves in the past.",
             "Use them when relevant, and never invent facts that are not listed here.", ""]
    for mem in picked:
        lines.append(f"- **{mem.key}**: {mem.value}")
    return ContextBlock(text="\n".join(lines), memories=picked)
