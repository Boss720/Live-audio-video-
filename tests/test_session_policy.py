"""LiveSession must not overwrite the tool policy installed by its owner (M3 A)."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from audit.journal import Journal
from core.commands import Disconnect
from core.events import ConnectionState, ConnectionStateChanged
from core.session import LiveSession
from persist.store import Store
from policy import PolicyEngine
from settings.settings import AppSettings
from tests.fakes.fake_live import FakeLive, make_fake_client_factory
from tools import _state as tools_state


class _FakeDispatcher:
    def __init__(self) -> None:
        self.respond_callbacks: list[Any] = []

    async def submit(self, call_id: str, name: str, args: dict[str, Any]) -> None:
        return None

    def spawn(self, coro: Any) -> None:
        asyncio.create_task(coro)

    def cancel(self, call_ids: list[str]) -> None:
        pass

    def set_respond_callback(self, callback: Any) -> None:
        self.respond_callbacks.append(callback)


async def _run_until_live(
    monkeypatch: Any, tmp_path: Path, *, dispatcher: Any | None
) -> tuple[LiveSession, Any]:
    live = FakeLive()
    monkeypatch.setattr("google.genai.Client", make_fake_client_factory(live))
    monkeypatch.setattr("settings.secrets.get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr("media.devices.list_input_devices", lambda: [])
    monkeypatch.setattr("media.devices.list_output_devices", lambda: [])
    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    event_queue: asyncio.Queue[Any] = asyncio.Queue()
    session_root = tmp_path / "session_root"
    session_root.mkdir()
    settings = AppSettings(
        model="gemini-3.1-flash-live-preview",
        voice="Puck",
        mode="text",
        working_dir=session_root,
        workspace_root=session_root,
        share_screen=False,
        computer_use_enabled=False,
    )
    session = LiveSession(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=Journal(run_id="policy-test", log_dir=tmp_path),
        store=Store(db_path=tmp_path / "policy.db"),
        run_id="policy-test",
        dispatcher=dispatcher,
    )
    task = asyncio.create_task(session.run())
    try:
        while True:
            event = await asyncio.wait_for(event_queue.get(), timeout=5)
            if (
                isinstance(event, ConnectionStateChanged)
                and event.state == ConnectionState.LIVE
            ):
                break
        await asyncio.sleep(0.05)
        observed = (tools_state._policy, tools_state._workspace_root)
        command_queue.put_nowait(Disconnect())
        await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        session._journal._close()
    return session, observed


@pytest.fixture
def restore_tools_state() -> Any:
    saved_policy = tools_state._policy
    saved_root = tools_state._workspace_root
    saved_dynamic = tools_state._active_dynamic_workspace_root
    yield
    tools_state._policy = saved_policy
    tools_state._workspace_root = saved_root
    tools_state._active_dynamic_workspace_root = saved_dynamic


@pytest.mark.asyncio
async def test_injected_dispatcher_keeps_owner_policy(
    monkeypatch: Any, tmp_path: Path, restore_tools_state: Any
) -> None:
    owner_root = tmp_path / "owner_root"
    owner_root.mkdir()
    owner_policy = PolicyEngine(
        allow_roots=[owner_root],
        deny_globs=[r"*\.git*", "__pycache__"],
        never_allow_regex=[],
    )
    tools_state.set_policy(owner_policy)
    tools_state.set_workspace(owner_root)

    dispatcher = _FakeDispatcher()
    _, (policy, root) = await _run_until_live(
        monkeypatch, tmp_path, dispatcher=dispatcher
    )

    assert policy is owner_policy
    assert root == owner_root.resolve()
    # The session must not rewire an injected dispatcher's respond path.
    assert dispatcher.respond_callbacks == []


@pytest.mark.asyncio
async def test_owned_dispatcher_installs_session_policy(
    monkeypatch: Any, tmp_path: Path, restore_tools_state: Any
) -> None:
    previous = PolicyEngine(allow_roots=[tmp_path], deny_globs=[], never_allow_regex=[])
    tools_state.set_policy(previous)

    session, (policy, root) = await _run_until_live(
        monkeypatch, tmp_path, dispatcher=None
    )

    assert policy is not previous
    assert root == (tmp_path / "session_root").resolve()
    # The session-built dispatcher and the tool-level checks share one engine.
    assert session._dispatcher is not None
    assert session._dispatcher._policy is policy
