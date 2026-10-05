"""Regression tests for wake-gated tool dispatch and frame serialization.

Covers:
- Wake gating evaluation on tool-only model responses (without text/audio).
- Protection against premature wake-gate reset when turn_complete arrives in the
  same message as a tool_call.
- Normal dispatch of tool calls when utterance is addressed with wake word.
- Frame send serialization under concurrent triggers.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from audit.journal import Journal
from core.commands import Disconnect
from core.events import ConnectionState, ConnectionStateChanged, SessionError
from core.session import LiveSession
from persist.store import Store
from settings.settings import AppSettings
from tests.fakes.fake_live import FakeLive, make_fake_client_factory


class _RecordingDispatcher:
    """Mock dispatcher that records submitted tool calls."""

    def __init__(self) -> None:
        self.submissions: list[tuple[str, str, dict[str, Any]]] = []

    async def submit(self, call_id: str, name: str, args: dict[str, Any]) -> Any:
        self.submissions.append((call_id, name, args))

    def spawn(self, coro: Any) -> None:
        asyncio.create_task(coro)

    def cancel(self, call_ids: list[str]) -> None:
        pass


def _make_settings(tmp_path: Path, **overrides: Any) -> AppSettings:
    defaults: dict[str, Any] = {
        "model": "gemini-3.1-flash-live-preview",
        "voice": "Puck",
        "mode": "both",
        "working_dir": tmp_path,
        "workspace_root": tmp_path,
        "wake_word_enabled": True,
        "wake_word": "Bongo",
        "share_screen": False,
        "computer_use_enabled": False,
    }
    defaults.update(overrides)
    return AppSettings(**defaults)


async def _run_session(
    monkeypatch: Any,
    tmp_path: Path,
    settings: AppSettings,
) -> tuple[FakeLive, LiveSession, asyncio.Queue[Any], asyncio.Queue[Any], _RecordingDispatcher]:
    live = FakeLive()
    monkeypatch.setattr("google.genai.Client", make_fake_client_factory(live))
    monkeypatch.setattr("settings.secrets.get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr("media.devices.list_input_devices", lambda: [])
    monkeypatch.setattr("media.devices.list_output_devices", lambda: [])
    monkeypatch.setattr("media.screen.capture_jpeg", lambda *a, **k: b"jpeg")
    monkeypatch.setattr("core.session.capture_jpeg", lambda *a, **k: b"jpeg")

    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    event_queue: asyncio.Queue[Any] = asyncio.Queue()
    journal = Journal(run_id="wake-tool-test", log_dir=tmp_path)
    store = Store(db_path=tmp_path / "wake_tool.db")
    session = LiveSession(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=store,
        run_id="wake-tool-test",
    )
    dispatcher = _RecordingDispatcher()
    session._dispatcher = dispatcher  # type: ignore[assignment]
    return live, session, command_queue, event_queue, dispatcher


async def _wait_for_live(queue: asyncio.Queue[Any]) -> None:
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=5)
        if (
            isinstance(event, ConnectionStateChanged)
            and event.state == ConnectionState.LIVE
        ):
            return


@pytest.mark.asyncio
async def test_wake_gate_blocks_tool_only_unaddressed_speech(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Unaddressed voice speech triggering a direct tool call must NOT dispatch."""
    settings = _make_settings(tmp_path, wake_word_enabled=True, wake_word="Bongo")
    live, session, command_queue, event_queue, dispatcher = await _run_session(
        monkeypatch, tmp_path, settings
    )

    task = asyncio.create_task(session.run())
    try:
        await _wait_for_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        # 1. Server sends unaddressed input transcription.
        fake_session.emit(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(
                        text="did you see the game last night"
                    )
                )
            )
        )
        # 2. Server sends tool call ONLY (no model text/audio).
        fake_session.request_tool("call-1", "read_file", {"path": "notes.txt"})

        await asyncio.sleep(0.15)
        command_queue.put_nowait(Disconnect())
        await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        session._journal._close()

    # Dispatcher must NOT have received the tool call submission.
    assert dispatcher.submissions == []

    events: list[Any] = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())

    # SessionError was surfaced indicating dropped turn.
    assert any(
        isinstance(e, SessionError) and "wake-word gate" in e.message
        for e in events
    )


@pytest.mark.asyncio
async def test_wake_gate_allows_tool_only_addressed_speech(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Addressed voice speech triggering a tool call must dispatch normally."""
    settings = _make_settings(tmp_path, wake_word_enabled=True, wake_word="Bongo")
    live, session, command_queue, event_queue, dispatcher = await _run_session(
        monkeypatch, tmp_path, settings
    )

    task = asyncio.create_task(session.run())
    try:
        await _wait_for_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        # 1. Addressed user input.
        fake_session.emit(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(
                        text="Hey Bongo, read my notes"
                    )
                )
            )
        )
        # 2. Server sends tool call.
        fake_session.request_tool("call-2", "read_file", {"path": "notes.txt"})

        await asyncio.sleep(0.15)
        command_queue.put_nowait(Disconnect())
        await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        session._journal._close()

    assert len(dispatcher.submissions) == 1
    assert dispatcher.submissions[0] == ("call-2", "read_file", {"path": "notes.txt"})


@pytest.mark.asyncio
async def test_wake_gate_turn_complete_in_same_message_as_tool_call(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """turn_complete in the same response as tool_call must not reset gate prematurely."""
    settings = _make_settings(tmp_path, wake_word_enabled=True, wake_word="Bongo")
    live, session, command_queue, event_queue, dispatcher = await _run_session(
        monkeypatch, tmp_path, settings
    )

    task = asyncio.create_task(session.run())
    try:
        await _wait_for_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        # Single message containing unaddressed speech, turn_complete, and tool call.
        fake_session.emit(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(
                        text="just talking to my colleague"
                    ),
                    turn_complete=True,
                ),
                tool_call=types.LiveServerToolCall(
                    function_calls=[
                        types.FunctionCall(
                            id="call-3",
                            name="read_file",
                            args={"path": "secret.txt"},
                        )
                    ]
                ),
            )
        )

        await asyncio.sleep(0.15)
        command_queue.put_nowait(Disconnect())
        await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        session._journal._close()

    # Must NOT dispatch.
    assert dispatcher.submissions == []


@pytest.mark.asyncio
async def test_send_frame_serialized_under_concurrent_calls(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Concurrent triggers to _send_frame must be strictly serialized by _frame_lock."""
    settings = _make_settings(tmp_path)
    live, session, command_queue, event_queue, dispatcher = await _run_session(
        monkeypatch, tmp_path, settings
    )

    task = asyncio.create_task(session.run())
    try:
        await _wait_for_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        active_sends = 0
        max_active_sends = 0
        original_send = fake_session.send_realtime_input

        async def tracking_send_realtime_input(*args: Any, **kwargs: Any) -> Any:
            nonlocal active_sends, max_active_sends
            active_sends += 1
            max_active_sends = max(max_active_sends, active_sends)
            await asyncio.sleep(0.02)
            active_sends -= 1
            return await original_send(*args, **kwargs)

        fake_session.send_realtime_input = tracking_send_realtime_input  # type: ignore[method-assign]

        results = await asyncio.gather(
            *[
                session._send_frame(fake_session, reason=f"concurrent-{i}", force=True)
                for i in range(5)
            ]
        )

        assert all(r is True for r in results)
        assert max_active_sends == 1

        command_queue.put_nowait(Disconnect())
        await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        session._journal._close()
