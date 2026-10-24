"""Transcript delivery through a fake Live socket, without media devices."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from audit.journal import Journal
from core.commands import Disconnect
from core.events import (
    ConnectionState,
    ConnectionStateChanged,
    PartialTranscript,
    TranscriptSource,
    TurnComplete,
)
from core.session import LiveSession
from core.turn_state import TurnState
from persist.store import Store
from settings.settings import AppSettings
from tests.fakes.fake_live import FakeLive, FakeLiveSession, make_fake_client_factory


@asynccontextmanager
async def _running_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    wake: bool = False,
    coalesce_ms: float = 80.0,
) -> AsyncIterator[
    tuple[FakeLiveSession, LiveSession, asyncio.Queue[Any], Store, asyncio.Task[Any]]
]:
    live = FakeLive()
    monkeypatch.setattr("google.genai.Client", make_fake_client_factory(live))
    monkeypatch.setattr("core.session.get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr("media.devices.list_input_devices", lambda: [])
    monkeypatch.setattr("media.devices.list_output_devices", lambda: [])
    settings = AppSettings(
        working_dir=tmp_path,
        workspace_root=tmp_path,
        mode="text",
        share_screen=False,
        computer_use_enabled=False,
        wake_word_enabled=wake,
        wake_word="Bongo",
    )
    commands: asyncio.Queue[Any] = asyncio.Queue()
    events: asyncio.Queue[Any] = asyncio.Queue()
    journal = Journal(run_id="transcript-test", log_dir=tmp_path)
    store = Store(db_path=tmp_path / "transcripts.db")
    store.start_run(
        "transcript-test", datetime.now(UTC).isoformat(), settings.model, "text", str(tmp_path)
    )
    session = LiveSession(
        settings=settings,
        command_queue=commands,
        event_queue=events,
        journal=journal,
        store=store,
        run_id="transcript-test",
        turn_state=TurnState(coalesce_ms=coalesce_ms),
    )
    task = asyncio.create_task(session.run())
    try:
        while True:
            event = await asyncio.wait_for(events.get(), timeout=2)
            if isinstance(event, ConnectionStateChanged) and event.state == ConnectionState.LIVE:
                break
        socket = live.latest_session
        assert socket is not None
        yield socket, session, events, store, task
    finally:
        if not task.done():
            commands.put_nowait(Disconnect())
        try:
            await asyncio.wait_for(task, timeout=2)
        except asyncio.CancelledError:
            pass
        finally:
            journal._close()
            store.close()


@pytest.mark.asyncio
async def test_partial_arrives_after_one_packet_followed_by_silence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async with _running_session(monkeypatch, tmp_path) as (socket, _, events, _, _):
        socket.say("single packet")
        event = await asyncio.wait_for(events.get(), timeout=0.5)
        assert isinstance(event, PartialTranscript)
        assert event.text == "single packet"


@pytest.mark.asyncio
async def test_coalesced_partial_precedes_final_once_and_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async with _running_session(monkeypatch, tmp_path) as (socket, _, events, store, _):
        socket.say("one ")
        socket.say("two")
        partial = await asyncio.wait_for(events.get(), timeout=0.5)
        assert partial == PartialTranscript(
            text="one two", source=TranscriptSource.MODEL, turn_id=0
        )
        socket.say(" three", turn_complete=True)
        tail = await asyncio.wait_for(events.get(), timeout=0.5)
        final = await asyncio.wait_for(events.get(), timeout=0.5)
        assert tail == PartialTranscript(text=" three", source=TranscriptSource.MODEL, turn_id=0)
        assert final == TurnComplete(text="one two three", source=TranscriptSource.MODEL, turn_id=0)
        await asyncio.sleep(0.12)
        assert events.empty()
        assert store.get_turns("transcript-test") == [("model", "one two three", 0)]


@pytest.mark.asyncio
async def test_wake_gate_holds_user_partial_until_addressing_is_known(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async with _running_session(monkeypatch, tmp_path, wake=True) as (socket, _, events, store, _):
        socket.emit(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(text="talking to a colleague")
                )
            )
        )
        await asyncio.sleep(0.12)
        assert events.empty()
        socket.say("should be dropped", turn_complete=True)
        await asyncio.sleep(0.12)
        seen = []
        while not events.empty():
            seen.append(events.get_nowait())
        assert not any(isinstance(event, (PartialTranscript, TurnComplete)) for event in seen)
        assert store.get_turns("transcript-test") == []

        socket.emit(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(text="Hey Bongo, hello")
                )
            )
        )
        await asyncio.sleep(0.12)
        assert events.empty()
        socket.say("hello")
        user_partial = await asyncio.wait_for(events.get(), timeout=0.5)
        model_partial = await asyncio.wait_for(events.get(), timeout=0.5)
        assert user_partial.text == "Hey Bongo, hello"
        assert user_partial.source == TranscriptSource.USER
        assert model_partial.text == "hello"
        assert model_partial.source == TranscriptSource.MODEL


@pytest.mark.asyncio
async def test_cancel_clears_timers_callback_and_delivery_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async with _running_session(monkeypatch, tmp_path, coalesce_ms=1000) as (
        socket,
        session,
        _,
        _,
        task,
    ):
        socket.say("pending")
        # Wait until the receive loop has scheduled a real coalescing timer.
        for _ in range(20):
            if session._turn_state._timer_model is not None:
                break
            await asyncio.sleep(0.01)
        assert session._turn_state._timer_model is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session._turn_state._timer_user is None
        assert session._turn_state._timer_model is None
        assert session._turn_state._timer_callback is None
        assert not any(
            pending.get_name() == "transcript_delivery" for pending in asyncio.all_tasks()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("wake", [False, True])
async def test_close_flushes_only_allowed_pending_transcripts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wake: bool
) -> None:
    async with _running_session(
        monkeypatch, tmp_path, wake=wake, coalesce_ms=1000
    ) as (socket, session, events, _, task):
        socket.emit(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(text="pending speech")
                )
            )
        )
        for _ in range(20):
            if session._turn_state._timer_user is not None:
                break
            await asyncio.sleep(0.01)
        assert session._turn_state._timer_user is not None
        session._command_queue.put_nowait(Disconnect())
        await asyncio.wait_for(task, timeout=2)
        partials = []
        while not events.empty():
            event = events.get_nowait()
            if isinstance(event, PartialTranscript):
                partials.append(event)
        assert [event.text for event in partials] == ([] if wake else ["pending speech"])
        assert session._turn_state._timer_user is None
        assert session._turn_state._timer_callback is None
        assert not session._undecided_turn_events
