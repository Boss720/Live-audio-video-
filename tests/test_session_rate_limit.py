"""Rate-limit (429) detection and ``generation_complete`` handling tests.

A Live API session can be terminated by quota exhaustion either as an
in-band error (SDK raises ``ClientError`` with code 429 / status
``RESOURCE_EXHAUSTED``) or as a WebSocket close whose reason string the SDK
wraps in a plain ``APIError``. Both must surface exactly one ``RateLimitHit``
event so the supervisor can cascade to another tier. A normal close must
surface none. Separately, a standalone ``generation_complete`` message must
close out the model turn exactly like ``turn_complete``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from google.genai.errors import APIError, ClientError

from audit.journal import Journal
from core.commands import Disconnect
from core.events import DisconnectReason, RateLimitHit, TranscriptSource, TurnComplete
from core.session import LiveSession
from persist.store import Store
from settings.settings import AppSettings
from tests.fakes.fake_live import FakeLive, FakeSessionError, make_fake_client_factory


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
    settings: AppSettings, tmp_path: Path
) -> tuple[LiveSession, asyncio.Queue[Any], asyncio.Queue[Any], Journal]:
    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    event_queue: asyncio.Queue[Any] = asyncio.Queue()
    journal = Journal(run_id="rate-limit", log_dir=tmp_path)
    session = LiveSession(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=Store(db_path=tmp_path / "state.db"),
        run_id="rate-limit",
    )
    return session, command_queue, event_queue, journal


def _drain(queue: asyncio.Queue[Any]) -> list[Any]:
    events: list[Any] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


# ----------------------------------------------------------------------
# Rate-limit detection
# ----------------------------------------------------------------------


async def test_rate_limit_error_emits_exactly_one_rate_limit_hit(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    live = FakeLive()
    _patch_common(monkeypatch, live)

    async def drive(fake_session: Any) -> None:
        fake_session.fail_next_receive(
            ClientError(
                429,
                {
                    "error": {
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "Quota exceeded for the current tier",
                    }
                },
            )
        )

    live.on_connect(drive)
    session, _commands, event_queue, journal = _make_session(settings, tmp_path)
    try:
        reason = await asyncio.wait_for(session.run(), timeout=5)
    finally:
        journal._close()

    hits = [e for e in _drain(event_queue) if isinstance(e, RateLimitHit)]
    assert len(hits) == 1
    hit = hits[0]
    assert hit.model == settings.model
    # The session does not track tiers/TPM; the supervisor fills these in.
    assert hit.tier == ""
    assert hit.tpm_used == 0
    assert hit.tpm_limit == 0
    assert hit.cooldown_seconds == 0.0
    assert reason.code == "quota"


async def test_disconnect_reason_is_emitted_as_event(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    live = FakeLive()
    _patch_common(monkeypatch, live)

    async def drive(fake_session: Any) -> None:
        fake_session.fail_next_receive(
            ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}})
        )

    live.on_connect(drive)
    session, _commands, event_queue, journal = _make_session(settings, tmp_path)
    try:
        reason = await asyncio.wait_for(session.run(), timeout=5)
    finally:
        journal._close()
    emitted = [e for e in _drain(event_queue) if isinstance(e, DisconnectReason)]
    assert emitted == [reason]


async def test_normal_close_emits_no_rate_limit_hit(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    live = FakeLive()
    _patch_common(monkeypatch, live)

    async def drive(fake_session: Any) -> None:
        fake_session.say("hello", turn_complete=True)

    live.on_connect(drive)
    session, command_queue, event_queue, journal = _make_session(settings, tmp_path)
    task = asyncio.create_task(session.run())
    try:
        # Wait until the turn arrives so the receive loop is exercised, then
        # end the session the ordinary way.
        while True:
            event = await asyncio.wait_for(event_queue.get(), timeout=5)
            if isinstance(event, TurnComplete):
                break
        await command_queue.put(Disconnect())
        reason = await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        journal._close()

    events = _drain(event_queue)
    assert not [e for e in events if isinstance(e, RateLimitHit)]
    assert reason.code == "user_request"


def test_is_rate_limit_matches_the_sdk_error_shapes() -> None:
    # In-band 429: ClientError with code/status set from the error body.
    assert LiveSession._is_rate_limit(
        ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "x"}})
    )
    # WebSocket close: the SDK wraps the close code + reason string in a
    # plain APIError (close codes are 1xxx, so no ClientError subclass),
    # leaving only the message text to match on.
    assert LiveSession._is_rate_limit(
        APIError(1008, "RESOURCE_EXHAUSTED: quota exceeded, closing connection")
    )
    assert LiveSession._is_rate_limit(Exception("429 Too Many Requests"))
    assert LiveSession._is_rate_limit(Exception("rate limit exceeded"))
    assert LiveSession._is_rate_limit(Exception("You exceeded your current quota"))
    assert not LiveSession._is_rate_limit(FakeSessionError("connection dropped"))
    assert not LiveSession._is_rate_limit(Exception("service unavailable"))


# ----------------------------------------------------------------------
# generation_complete
# ----------------------------------------------------------------------


async def test_generation_complete_advances_turn_state_like_turn_complete(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    live = FakeLive()
    _patch_common(monkeypatch, live)

    async def drive(fake_session: Any) -> None:
        # The model finishes generating without an accompanying turn_complete
        # (realtime playback delays it); the turn must still close out.
        fake_session.say("full answer")
        fake_session.complete_generation()
        fake_session.say("second answer", turn_complete=True)

    live.on_connect(drive)
    session, command_queue, event_queue, journal = _make_session(settings, tmp_path)
    task = asyncio.create_task(session.run())
    turns: list[TurnComplete] = []
    try:
        while len(turns) < 2:
            event = await asyncio.wait_for(event_queue.get(), timeout=5)
            if isinstance(event, TurnComplete):
                turns.append(event)
        await command_queue.put(Disconnect())
        await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        journal._close()

    assert [t.text for t in turns] == ["full answer", "second answer"]
    assert [t.turn_id for t in turns] == [0, 1]
    assert all(t.source == TranscriptSource.MODEL for t in turns)
