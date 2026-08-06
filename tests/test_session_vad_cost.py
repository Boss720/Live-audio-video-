"""Module 2: ``audio_stream_end`` signaling and session cost estimation.

Muting the mic closes the user's audio stream, so the session must send
``audio_stream_end=True`` inline (not queued behind audio chunks) when the
gate closes — and never when it opens. The same marker must be flushed once
during teardown if audio streamed and no marker was sent since. Separately,
each ``UsageUpdate`` must carry a cumulative ``estimated_usd`` computed from
the SDK's ``prompt_token_count`` / ``response_token_count`` at the per-model
paid-tier rates.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from audit.journal import Journal
from core.commands import Disconnect, SetMicGate
from core.events import ConnectionState, ConnectionStateChanged, UsageUpdate
from core.session import LiveSession, _cost_rates_for_model
from persist.store import Store
from settings.settings import AppSettings
from tests.fakes.fake_live import FakeLive, make_fake_client_factory


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        model="gemini-3.1-flash-live-preview",
        mode="text",
        working_dir=tmp_path,
        workspace_root=tmp_path,
        wake_word_enabled=False,
        computer_use_enabled=False,
        yolo=False,
        share_screen=False,
    )


def _patch_common(monkeypatch: Any, live: FakeLive) -> None:
    monkeypatch.setattr("google.genai.Client", make_fake_client_factory(live))
    monkeypatch.setattr("core.session.get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr("media.devices.list_input_devices", lambda: [])
    monkeypatch.setattr("media.devices.list_output_devices", lambda: [])


def _make_session(
    settings: AppSettings,
    tmp_path: Path,
    *,
    mic: Any | None = None,
) -> tuple[LiveSession, asyncio.Queue[Any], asyncio.Queue[Any], Journal]:
    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    event_queue: asyncio.Queue[Any] = asyncio.Queue()
    journal = Journal(run_id="vad-cost", log_dir=tmp_path)
    session = LiveSession(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=Store(db_path=tmp_path / "state.db"),
        run_id="vad-cost",
        mic=mic,
    )
    return session, command_queue, event_queue, journal


class _FakeMic:
    """Duck-typed stand-in for ``media.microphone.Microphone``."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.level = 0.0
        self.gate_open = True

    def set_gate(self, open: bool) -> None:  # noqa: A002 - matches Microphone
        self.gate_open = open

    def push(self, chunk: bytes) -> None:
        self._queue.put_nowait(chunk)

    async def chunks(self) -> Any:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item


async def _wait_live(event_queue: asyncio.Queue[Any]) -> None:
    while True:
        event = await asyncio.wait_for(event_queue.get(), timeout=5)
        if (
            isinstance(event, ConnectionStateChanged)
            and event.state == ConnectionState.LIVE
        ):
            return


async def _collect_until(
    fake_session: Any,
    predicate: Any,
) -> list[Any]:
    """Drain the fake outbox until ``predicate`` matches a collected message."""
    collected: list[Any] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        collected.extend(fake_session.pop_outbox())
        if any(predicate(message) for message in collected):
            return collected
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for a matching client message")


def _is_stream_end(message: Any) -> bool:
    return getattr(message, "audio_stream_end", None) is True


def _is_audio_chunk(message: Any) -> bool:
    return getattr(message, "audio", None) is not None


# ----------------------------------------------------------------------
# Mic gate -> audio_stream_end
# ----------------------------------------------------------------------
async def test_audio_stream_end_sent_on_gate_close_not_on_open(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    live = FakeLive()
    _patch_common(monkeypatch, live)
    session, command_queue, event_queue, journal = _make_session(settings, tmp_path)
    task = asyncio.create_task(session.run())
    try:
        await _wait_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        # Closing the gate sends the marker inline.
        await command_queue.put(SetMicGate(open=False))
        sent = await _collect_until(fake_session, _is_stream_end)
        assert len([m for m in sent if _is_stream_end(m)]) == 1

        # Opening the gate must not send the marker.
        await command_queue.put(SetMicGate(open=True))
        await asyncio.sleep(0.1)
        assert not [_is_stream_end(m) for m in fake_session.pop_outbox() if _is_stream_end(m)]

        # Closing again (an open->closed transition) sends it once more.
        await command_queue.put(SetMicGate(open=False))
        sent = await _collect_until(fake_session, _is_stream_end)
        assert len([m for m in sent if _is_stream_end(m)]) == 1

        await command_queue.put(Disconnect())
        reason = await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        journal._close()

    assert reason.code == "user_request"
    # No audio ever streamed, so teardown adds no further marker.
    assert not [m for m in fake_session.pop_outbox() if _is_stream_end(m)]


# ----------------------------------------------------------------------
# Teardown flush
# ----------------------------------------------------------------------
async def test_audio_stream_end_sent_once_on_teardown_after_audio(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    live = FakeLive()
    _patch_common(monkeypatch, live)
    mic = _FakeMic()
    session, command_queue, event_queue, journal = _make_session(
        settings, tmp_path, mic=mic
    )
    task = asyncio.create_task(session.run())
    sent: list[Any] = []
    try:
        await _wait_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        mic.push(b"\x01\x00" * 160)
        sent = await _collect_until(fake_session, _is_audio_chunk)
        assert not [m for m in sent if _is_stream_end(m)]

        await command_queue.put(Disconnect())
        reason = await asyncio.wait_for(task, timeout=5)
        sent.extend(fake_session.pop_outbox())
    finally:
        task.cancel()
        journal._close()

    assert reason.code == "user_request"
    assert len([m for m in sent if _is_stream_end(m)]) == 1


async def test_no_audio_stream_end_on_teardown_without_audio(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    live = FakeLive()
    _patch_common(monkeypatch, live)
    session, command_queue, event_queue, journal = _make_session(settings, tmp_path)
    task = asyncio.create_task(session.run())
    try:
        await _wait_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        await command_queue.put(Disconnect())
        reason = await asyncio.wait_for(task, timeout=5)
        sent = fake_session.pop_outbox()
    finally:
        task.cancel()
        journal._close()

    assert reason.code == "user_request"
    assert not [m for m in sent if _is_stream_end(m)]


# ----------------------------------------------------------------------
# estimated_usd cost tracking
# ----------------------------------------------------------------------
async def test_usage_update_carries_cumulative_estimated_usd(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    live = FakeLive()
    _patch_common(monkeypatch, live)
    session, command_queue, event_queue, journal = _make_session(settings, tmp_path)
    task = asyncio.create_task(session.run())
    updates: list[UsageUpdate] = []
    try:
        await _wait_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        fake_session.usage(
            prompt_token_count=1_000_000,
            response_token_count=500_000,
            total_token_count=1_500_000,
        )
        fake_session.usage(
            prompt_token_count=1_000_000,
            response_token_count=0,
            total_token_count=1_000_000,
        )

        while len(updates) < 2:
            event = await asyncio.wait_for(event_queue.get(), timeout=5)
            if isinstance(event, UsageUpdate):
                updates.append(event)

        await command_queue.put(Disconnect())
        await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        journal._close()

    # 1M input * $0.15 + 0.5M output * $0.60 = $0.45
    assert updates[0].estimated_usd == pytest.approx(0.45)
    # Cumulative: +1M input * $0.15 = $0.60
    assert updates[1].estimated_usd == pytest.approx(0.60)


def test_cost_rates_for_model_prefixes() -> None:
    assert _cost_rates_for_model("gemini-3.1-flash-live-preview") == (0.15, 0.60)
    assert _cost_rates_for_model("gemini-2.5-flash-native-audio") == (0.15, 0.60)
    # Unknown models fall back to the default rates.
    assert _cost_rates_for_model("some-future-model") == (0.15, 0.60)


def test_usage_update_estimated_usd_defaults_to_none() -> None:
    # None = free tier / unknown, so the GUI can show "Free".
    assert UsageUpdate().estimated_usd is None
    assert types.LiveClientRealtimeInput(audio_stream_end=True).audio_stream_end is True
