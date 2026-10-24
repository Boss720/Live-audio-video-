"""Screen freshness and opt-in streaming deduplication, using synthetic bytes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from core.commands import Disconnect, SendText
from core.events import SessionError
from settings.settings import AppSettings
from tests.fakes.fake_live import FakeLive
from tests.test_frame_gating import _make_session, _patch_common, _wait_live


class Socket:
    def __init__(self) -> None:
        self.videos: list[bytes] = []
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.texts: list[str] = []

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.videos.append(kwargs["video"].data)

    async def receive(self) -> Any:
        while (message := await self.inbox.get()) is not None:
            yield message

    async def send_client_content(self, **kwargs: Any) -> None:
        self.texts.append(kwargs["turns"][0].parts[0].text)

    def transcribe(self, text: str) -> None:
        self.inbox.put_nowait(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(text=text)
                )
            )
        )


def settings_for(tmp_path: Path, **overrides: Any) -> AppSettings:
    values: dict[str, Any] = {
        "working_dir": tmp_path,
        "workspace_root": tmp_path,
        "mode": "text",
        "frame_mode": "on_demand",
        "frame_heartbeat_seconds": 60,
        "share_screen": True,
        "computer_use_enabled": False,
        "wake_word_enabled": False,
    }
    values.update(overrides)
    return AppSettings(**values)


async def settle_frames(session: Any) -> None:
    # Let the receive loop schedule its frame request, then wait for capture.
    for _ in range(5):
        await asyncio.sleep(0)
    if session._frame_tasks:
        await asyncio.gather(*session._frame_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("sharing,computer", [(True, False), (False, True)])
async def test_typed_input_refreshes_before_text_immediately_after_connect(
    tmp_path: Path,
    monkeypatch: Any,
    sharing: bool,
    computer: bool,
) -> None:
    settings = settings_for(tmp_path, share_screen=sharing, computer_use_enabled=computer)
    live = FakeLive()
    _patch_common(monkeypatch, live)
    session, commands, events, journal = _make_session(settings, tmp_path)
    runner = asyncio.create_task(session.run())
    try:
        await _wait_live(events)
        await commands.put(SendText(text="Please help with this"))
        await commands.put(Disconnect())
        await asyncio.wait_for(runner, 5)
        socket = live.latest_session
        assert socket is not None
        messages = socket.pop_outbox()
        frames = [
            i
            for i, message in enumerate(messages)
            if isinstance(message, types.LiveClientRealtimeInput) and message.video
        ]
        texts = [
            i for i, message in enumerate(messages) if isinstance(message, types.LiveClientContent)
        ]
        assert frames and texts and frames[0] < texts[0]
        assert not session._frame_tasks
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        journal.close()


@pytest.mark.asyncio
async def test_voice_after_heartbeat_bypasses_floor_but_retries_capture_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    session, _, _, journal = _make_session(settings_for(tmp_path), tmp_path)
    socket = Socket()
    frames = iter([b"heartbeat", RuntimeError("capture unavailable"), b"current"])

    def capture(*_args: Any, **_kwargs: Any) -> bytes:
        result = next(frames)
        if isinstance(result, RuntimeError):
            raise result
        return result

    monkeypatch.setattr("core.session.capture_jpeg", capture)
    await session._send_frame(socket, reason="heartbeat")
    receiver = asyncio.create_task(session._receive_loop(socket))
    try:
        socket.transcribe("Please")
        await settle_frames(session)
        assert not session._frame_sent_this_utterance
        socket.transcribe(" inspect this")
        await settle_frames(session)
        assert socket.videos == [b"heartbeat", b"current"]
        assert session._frame_sent_this_utterance
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        await session._stop_media()
        journal.close()


@pytest.mark.asyncio
async def test_competing_user_requests_capture_once_and_keep_digest_check(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    session, _, _, journal = _make_session(settings_for(tmp_path), tmp_path)
    socket = Socket()
    captures = 0

    def capture(*_args: Any, **_kwargs: Any) -> bytes:
        nonlocal captures
        captures += 1
        return b"unchanged"

    monkeypatch.setattr("core.session.capture_jpeg", capture)
    try:
        await session._send_frame(socket, reason="heartbeat")
        for _ in range(5):
            session._spawn_frame(socket, reason="user_turn")
        await settle_frames(session)
        assert captures == 2  # baseline plus one fresh check, even inside floor
        assert socket.videos == [b"unchanged"]
        assert session._frame_sent_this_utterance
    finally:
        await session._stop_media()
        journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deduplicate,expected",
    [
        (False, [b"a", b"a", b"b", b"b", b"b"]),
        (True, [b"a", b"b", b"b"]),
    ],
)
async def test_stream_repeated_changed_and_heartbeat_refresh(
    tmp_path: Path,
    monkeypatch: Any,
    deduplicate: bool,
    expected: list[bytes],
) -> None:
    settings = settings_for(
        tmp_path, frame_mode="stream", screen_deduplicate=deduplicate, frame_heartbeat_seconds=8
    )
    session, _, _, journal = _make_session(settings, tmp_path)
    socket = Socket()
    clock = [100.0]
    monkeypatch.setattr("core.session.time", SimpleNamespace(monotonic=lambda: clock[0]))
    frames = iter([(100, b"a"), (101, b"a"), (102, b"b"), (109, b"b"), (110, b"b")])

    class Screen:
        async def read_frame(self) -> bytes:
            result = next(frames, None)
            if result is None:
                raise asyncio.CancelledError
            clock[0], frame = result
            return frame

    try:
        await session._video_send_loop(socket, Screen())
        assert socket.videos == expected
        assert session._metrics.counters()["frames_sent"] == len(expected)
        assert session._metrics.counters()["frame_bytes_sent"] == len(expected)
        if deduplicate:
            assert session._metrics.counters()["frame_duplicates_dropped"] == 2
    finally:
        journal.close()


def test_stream_dedup_is_disabled_by_default(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    assert settings.screen_deduplicate is False
    assert settings.screen_fps == 1.0
    assert settings.media_resolution == "high"


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_user_frame_tasks(tmp_path: Path) -> None:
    session, _, _, journal = _make_session(settings_for(tmp_path), tmp_path)
    socket = Socket()
    await session._frame_lock.acquire()
    try:
        session._spawn_frame(socket, reason="user_turn")
        await asyncio.sleep(0)
        tasks = list(session._frame_tasks)
        await session._stop_media()
        assert tasks and all(task.done() for task in tasks)
        assert not session._frame_tasks
        assert not session._frame_sent_this_utterance
    finally:
        session._frame_lock.release()
        journal.close()


@pytest.mark.asyncio
async def test_typed_capture_failure_keeps_text_unsent_and_retryable(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    session, commands, events, journal = _make_session(settings_for(tmp_path), tmp_path)
    socket = Socket()

    def unavailable(*_args: Any, **_kwargs: Any) -> bytes:
        raise RuntimeError("synthetic capture failure")

    monkeypatch.setattr("core.session.capture_jpeg", unavailable)
    sender = asyncio.create_task(session._send_loop(socket))
    try:
        await commands.put(SendText(text="inspect this"))
        error = await asyncio.wait_for(events.get(), 5)
        assert isinstance(error, SessionError) and "text was not sent" in error.message
        assert socket.texts == []
        assert not session._frame_sent_this_utterance
        monkeypatch.setattr("core.session.capture_jpeg", lambda *_args, **_kw: b"current")
        await commands.put(SendText(text="inspect this"))
        await commands.put(Disconnect())
        await asyncio.wait_for(sender, 5)
        assert socket.videos == [b"current"]
        assert socket.texts == ["inspect this"]
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        await session._stop_media()
        journal.close()


@pytest.mark.asyncio
async def test_failed_send_does_not_commit_digest_or_metrics(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    session, _, _, journal = _make_session(settings_for(tmp_path), tmp_path)
    monkeypatch.setattr("core.session.capture_jpeg", lambda *_args, **_kw: b"retry")

    class FailingOnce(Socket):
        failed = False

        async def send_realtime_input(self, **kwargs: Any) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("synthetic send failure")
            await super().send_realtime_input(**kwargs)

    socket = FailingOnce()
    try:
        assert not await session._spawn_frame(socket, reason="user_turn")
        assert not session._frame_sent_this_utterance
        assert session._last_frame_digest is None
        assert session._last_frame_sent == 0
        assert session._metrics.counters().get("frames_sent", 0) == 0
        assert await session._spawn_frame(socket, reason="user_turn")
        assert socket.videos == [b"retry"]
        assert session._metrics.counters()["frames_sent"] == 1
        assert session._metrics.counters()["frame_bytes_sent"] == 5
    finally:
        await session._stop_media()
        journal.close()


@pytest.mark.asyncio
async def test_stream_queued_before_requested_capture_does_not_overwrite_it(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    session, _, _, journal = _make_session(
        settings_for(tmp_path, frame_mode="stream", screen_deduplicate=True), tmp_path
    )
    socket = Socket()
    monkeypatch.setattr("core.session.capture_jpeg", lambda *_args, **_kw: b"current")
    read = asyncio.Event()

    class Screen:
        async def read_frame(self) -> bytes:
            if read.is_set():
                raise asyncio.CancelledError
            read.set()
            return b"queued-old-frame"

    await session._frame_lock.acquire()
    requested = asyncio.create_task(session._send_frame(socket, reason="user_turn"))
    await asyncio.sleep(0)  # Queue the requested capture first.
    streaming = asyncio.create_task(session._video_send_loop(socket, Screen()))
    try:
        await asyncio.wait_for(read.wait(), 5)
        session._frame_lock.release()
        await asyncio.wait_for(asyncio.gather(requested, streaming), 5)
        assert socket.videos == [b"current"]
        assert session._metrics.counters()["frames_sent"] == 1
    finally:
        requested.cancel()
        streaming.cancel()
        await asyncio.gather(requested, streaming, return_exceptions=True)
        journal.close()


@pytest.mark.asyncio
async def test_post_action_forces_unchanged_stream_frame_with_dedup_enabled(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    session, _, _, journal = _make_session(
        settings_for(tmp_path, frame_mode="stream", screen_deduplicate=True), tmp_path
    )
    socket = Socket()
    monkeypatch.setattr("core.session.capture_jpeg", lambda *_args, **_kw: b"unchanged")
    monkeypatch.setattr("core.session._POST_ACTION_SETTLE_SECONDS", 0)
    try:
        await session._send_frame(socket, reason="heartbeat")
        await session._push_fresh_frame(socket)
        assert socket.videos == [b"unchanged", b"unchanged"]
        assert session._metrics.counters()["frames_sent"] == 2
    finally:
        journal.close()
