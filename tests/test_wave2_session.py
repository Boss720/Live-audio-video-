"""Wave 2 session behaviour: scheduling, frame wait, noise caps, usage, latency."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from core.commands import Disconnect, SendText
from core.events import AudioLevel, DiagnosticsUpdate, TextNotSent, UsageUpdate
from core.session import LiveSession, _response_scheduling_for
from obs.metrics import RuntimeMetrics
from persist.store import Store
from tests.test_frame_gating import _make_session
from tests.test_screen_freshness import Socket, settings_for

EXTENDED = "gemini-3.8-live-extended-thinking"


def _drain(queue: asyncio.Queue[Any]) -> list[Any]:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def _clock(session: LiveSession) -> list[float]:
    now = [100.0]
    session._metrics = RuntimeMetrics(clock=lambda: now[0])
    return now


# -- 1. response scheduling -------------------------------------------------


def test_extended_thinking_interrupts_for_computer_and_shell_tools() -> None:
    interrupt = types.FunctionResponseScheduling.INTERRUPT
    for name in ("computer_left_click", "computer_screenshot", "run_command"):
        assert _response_scheduling_for(name, EXTENDED) == interrupt
        assert _response_scheduling_for(name, "gemini-3.1-flash-live-preview") is None
    assert (
        _response_scheduling_for("read_file", EXTENDED)
        == types.FunctionResponseScheduling.WHEN_IDLE
    )
    assert (
        _response_scheduling_for("computer_mouse_move", EXTENDED)
        == types.FunctionResponseScheduling.SILENT
    )


# -- 2. bounded frame wait ----------------------------------------------------


@pytest.mark.asyncio
async def test_typed_text_sent_without_frame_after_timeout(
    tmp_path: Path, monkeypatch: Any
) -> None:
    session, commands, _, journal = _make_session(settings_for(tmp_path), tmp_path)
    monkeypatch.setattr("core.session._FRAME_WAIT_SECONDS", 0.05)
    release = asyncio.Event()

    async def slow_frame(*_args: Any, **_kwargs: Any) -> bool:
        await release.wait()
        return True

    monkeypatch.setattr(session, "_send_frame", slow_frame)
    socket = Socket()
    await commands.put(SendText(text="hello"))
    await commands.put(Disconnect())
    try:
        await asyncio.wait_for(session._send_loop(socket), 2)
        assert socket.texts == ["hello"]
        assert session._metrics.counters()["frame_wait_timeouts"] == 1
        # The frame task is not leaked: it is still tracked and cancellable.
        assert session._frame_tasks
        await session._stop_media()
        assert not session._frame_tasks
    finally:
        release.set()
        journal.close()


@pytest.mark.asyncio
async def test_failed_frame_still_blocks_text(tmp_path: Path, monkeypatch: Any) -> None:
    session, commands, events, journal = _make_session(settings_for(tmp_path), tmp_path)

    async def failing_frame(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("capture unavailable")

    monkeypatch.setattr(session, "_send_frame", failing_frame)
    socket = Socket()
    await commands.put(SendText(text="hello"))
    await commands.put(Disconnect())
    try:
        await asyncio.wait_for(session._send_loop(socket), 2)
        assert socket.texts == []
        assert any(isinstance(e, TextNotSent) for e in _drain(events))
    finally:
        journal.close()


# -- 3. event-queue noise caps ---------------------------------------------------


@pytest.mark.asyncio
async def test_audio_level_capped_at_15hz_and_only_on_change(tmp_path: Path) -> None:
    session, _, events, journal = _make_session(settings_for(tmp_path), tmp_path)
    now = _clock(session)
    try:
        await session._emit_level("mic", 0.5)
        now[0] += 0.01
        await session._emit_level("mic", 0.6)  # changed but inside 1/15 s
        now[0] += 0.1
        await session._emit_level("mic", 0.5)  # past interval, but unchanged
        await session._emit_level("mic", 0.7)  # changed and due
        await session._emit_level("speaker", 0.1)  # sources are independent
        levels = [(e.source, e.level) for e in _drain(events) if isinstance(e, AudioLevel)]
        assert levels == [("mic", 0.5), ("mic", 0.7), ("speaker", 0.1)]
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_diagnostics_capped_at_1hz_with_forced_final(tmp_path: Path) -> None:
    session, _, events, journal = _make_session(settings_for(tmp_path), tmp_path)
    now = _clock(session)
    try:
        await session._emit_diagnostics()
        now[0] += 0.5
        await session._emit_diagnostics()
        assert len(_drain(events)) == 1
        now[0] += 0.6
        await session._emit_diagnostics()
        await session._emit_diagnostics(force=True)
        assert [type(e) for e in _drain(events)] == [DiagnosticsUpdate, DiagnosticsUpdate]
    finally:
        journal.close()


# -- 4. usage contract -------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_tier_usage_is_unpriced(tmp_path: Path) -> None:
    session, _, events, journal = _make_session(settings_for(tmp_path), tmp_path)
    free = LiveSession(
        session._settings, asyncio.Queue(), asyncio.Queue(), journal,
        Store(db_path=tmp_path / "free.db"), "free", free_tier=True,
    )
    try:
        event = UsageUpdate(prompt_tokens=1000, completion_tokens=500, estimated_usd=1.0)
        session._record_usage(event)
        free._record_usage(event)
        paid_rows = session._store.get_usage("frames")
        free_rows = free._store.get_usage("free")
        assert any(row[2] > 0 for row in paid_rows)
        assert free_rows and all(row[2] == 0.0 for row in free_rows)
    finally:
        free._store.close()
        session._store.close()
        journal.close()


class _Usage:
    prompt_token_count = 100
    response_token_count = 50
    cached_content_token_count = 0
    total_token_count = 150
    prompt_tokens_details = None
    response_tokens_details = None


@pytest.mark.asyncio
@pytest.mark.parametrize("free_tier", [False, True])
async def test_usage_event_sums_increments_and_nulls_cost_when_free(
    tmp_path: Path, free_tier: bool
) -> None:
    session, _, events, journal = _make_session(settings_for(tmp_path), tmp_path)
    session._free_tier = free_tier
    socket = Socket()
    receiver = asyncio.create_task(session._receive_loop(socket))
    try:
        for _ in range(2):
            socket.inbox.put_nowait(types.LiveServerMessage.model_construct(
                usage_metadata=_Usage(), server_content=None, tool_call=None,
                go_away=None, session_resumption_update=None,
                tool_call_cancellation=None,
            ))
        await asyncio.sleep(0.05)
        usage = [e for e in _drain(events) if isinstance(e, UsageUpdate)]
        assert len(usage) == 2
        assert session._prompt_tokens_used == 200
        if free_tier:
            assert all(u.estimated_usd is None for u in usage)
        else:
            assert usage[1].estimated_usd and usage[1].estimated_usd > usage[0].estimated_usd
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        session._store.close()
        journal.close()


# -- 5. speech end -> first audio ----------------------------------------------------


class _Speaker:
    level = 0.0

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        pass


def _audio(interrupted: bool = False, turn_complete: bool = False) -> Any:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                parts=[types.Part(inline_data=types.Blob(data=b"pcm", mime_type="audio/pcm"))]
            ),
            interrupted=interrupted or None,
            turn_complete=turn_complete or None,
        )
    )


@pytest.mark.asyncio
async def test_speech_end_to_first_audio_recorded_once_per_turn(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, share_screen=False, mode="voice")
    session, _, _, journal = _make_session(settings, tmp_path)
    now = _clock(session)
    session._speaker = _Speaker()  # type: ignore[assignment]
    socket = Socket()
    receiver = asyncio.create_task(session._receive_loop(socket))

    async def feed(message: Any, advance: float = 0.0) -> None:
        now[0] += advance
        socket.inbox.put_nowait(message)
        for _ in range(5):
            await asyncio.sleep(0)

    try:
        socket.transcribe("hello")
        await asyncio.sleep(0)
        await feed(_audio(), advance=0.25)  # first audio 250 ms after speech end
        await feed(_audio(), advance=0.5)  # later chunks never re-record
        summary = session.runtime_diagnostics().timings["speech_end_to_first_audio_ms"]
        assert summary.samples == 1
        assert summary.latest_ms == pytest.approx(250.0)

        # Late transcription after audio started must not arm a second sample.
        socket.transcribe("late")
        await feed(_audio(), advance=0.1)
        assert session.runtime_diagnostics().timings["speech_end_to_first_audio_ms"].samples == 1

        # Turn boundary resets; an interrupted response records nothing.
        await feed(_audio(turn_complete=True))
        socket.transcribe("again")
        await asyncio.sleep(0)
        await feed(_audio(interrupted=True), advance=0.3)
        assert session.runtime_diagnostics().timings["speech_end_to_first_audio_ms"].samples == 1
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        session._store.close()
        journal.close()
