"""TUI smoke tests driven by Textual's headless pilot.

These don't assert on pixels; they prove the app mounts, the widget queries in
the event handlers resolve, and a full streamed turn renders without raising.
That's the class of bug (a bad selector, a renamed id) that unit tests miss.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pyassist.config import Config
from pyassist.core.bootstrap import AppContext
from pyassist.core.engine import ChatEngine
from pyassist.core.llm import LLMClient
from pyassist.core.storage import Storage
from pyassist.plugins.api import PluginRegistry
from pyassist.tools import build_default_registry
from pyassist.ui.app import PyAssistApp
from pyassist.ui.widgets import Composer, MessageBubble, NoticePanel, WelcomePanel

REPLY = "Hello from the stub."


def stub_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        frames = [
            json.dumps({"choices": [{"index": 0, "delta": {"content": word + " "}}]})
            for word in REPLY.split()
        ]
        frames.append(
            json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        )
        body = "".join(f"data: {f}\n\n" for f in frames) + "data: [DONE]\n\n"
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    return httpx.MockTransport(handler)


@pytest.fixture
async def context(tmp_path: Path):
    """A fully wired AppContext with a stubbed HTTP layer."""
    config = Config(
        api_key="k",
        base_url="https://api.test/v1",
        model="test-model",
        memory_enabled=False,
        tools_enabled=False,  # keep the transcript deterministic
    )
    storage = Storage(tmp_path / "db.sqlite3")
    await storage.connect()
    llm = LLMClient(config, httpx.AsyncClient(transport=stub_transport()))
    tools = build_default_registry()
    engine = ChatEngine(config, storage, llm, tools, workspace=tmp_path)

    ctx = AppContext(
        config=config,
        storage=storage,
        llm=llm,
        tools=tools,
        engine=engine,
        plugins=PluginRegistry(tools),
        workspace=tmp_path,
    )
    yield ctx
    await storage.close()


async def test_app_mounts_and_shows_welcome(context) -> None:
    app = PyAssistApp(context)
    async with app.run_test() as pilot:
        assert app.query(WelcomePanel)
        assert app.conversation is not None


async def test_sending_a_message_renders_both_bubbles(context) -> None:
    app = PyAssistApp(context)
    async with app.run_test() as pilot:
        await app._send("hi there")
        if app._stream_task is not None:
            await app._stream_task
        await pilot.pause()

        bubbles = list(app.query(MessageBubble))
        assert len(bubbles) >= 2, "expected a user and an assistant bubble"
        # The welcome panel must give way to the conversation.
        assert not app.query(WelcomePanel)


async def test_slash_help_opens_and_closes(context) -> None:
    """A modal must be *driven*, not awaited inline.

    ``_run_command("/help")`` does not return until the modal is dismissed, and
    the thing that dismisses it is a keypress delivered by the message pump.
    Awaiting it directly in the test body would deadlock for the same reason it
    would in the real app, so the command runs as a task while the pilot types.
    """
    app = PyAssistApp(context)
    async with app.run_test() as pilot:
        command = asyncio.create_task(app._run_command("/help"))
        await pilot.pause()
        assert len(app.screen_stack) > 1, "help screen should be on the stack"

        await pilot.press("escape")
        await command
        await pilot.pause()
        assert len(app.screen_stack) == 1, "help screen should have been popped"


async def test_help_from_composer_does_not_block_the_pump(context) -> None:
    """Regression: typing ``/help`` and pressing Enter must not deadlock.

    ``on_key`` hands submission to a worker precisely so the pump stays free to
    deliver the Escape that closes the modal.
    """
    app = PyAssistApp(context)
    async with app.run_test() as pilot:
        app.query_one(Composer).text = "/help"
        await pilot.press("enter")
        await pilot.pause()
        assert len(app.screen_stack) > 1, "Enter on /help should open the modal"

        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_unknown_command_shows_notice(context) -> None:
    app = PyAssistApp(context)
    async with app.run_test() as pilot:
        await app._run_command("/definitely-not-a-command")
        await pilot.pause()
        assert app.query(NoticePanel), "unknown commands should explain themselves"


async def test_toggle_sidebar_and_new_chat(context) -> None:
    app = PyAssistApp(context)
    async with app.run_test() as pilot:
        app.action_toggle_sidebar()
        await pilot.pause()
        first = app.conversation.id

        await app.action_new_chat()
        await pilot.pause()
        assert app.conversation.id != first


async def test_tools_command_lists_nothing_when_disabled(context) -> None:
    app = PyAssistApp(context)
    async with app.run_test() as pilot:
        await app._run_command("/tools")
        await pilot.pause()
        # tools_enabled=False in the fixture, so this must not raise.
        assert app.conversation is not None
