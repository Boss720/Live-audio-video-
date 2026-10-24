"""A rejected preflight returns the unsent text for explicit retry."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from audit.journal import Journal
from core.commands import Disconnect, SendText
from core.events import TextNotSent
from core.session import LiveSession
from persist.store import Store
from settings.settings import AppSettings


async def test_failed_screen_refresh_returns_unsent_draft(tmp_path: Path) -> None:
    settings = AppSettings(
        _env_file=None, working_dir=tmp_path, workspace_root=tmp_path,
        mode="text", frame_mode="on_demand", share_screen=True,
    )
    commands: asyncio.Queue = asyncio.Queue()
    events: asyncio.Queue = asyncio.Queue()
    journal = Journal(run_id="unsent", log_dir=tmp_path)
    store = Store(db_path=tmp_path / "state.db")
    session = LiveSession(settings, commands, events, journal, store, "unsent")
    future = asyncio.get_running_loop().create_future()
    future.set_result(False)
    session._spawn_frame = lambda *a, **kw: future  # type: ignore[method-assign,return-value]
    socket = AsyncMock()
    commands.put_nowait(SendText("Keep this long draft for retry"))
    commands.put_nowait(Disconnect())
    try:
        await session._send_loop(socket)
        socket.send_client_content.assert_not_awaited()
        returned = []
        while not events.empty():
            event = events.get_nowait()
            if isinstance(event, TextNotSent):
                returned.append(event)
        assert len(returned) == 1
        assert returned[0].text == "Keep this long draft for retry"
    finally:
        journal.close()
        store.close()
