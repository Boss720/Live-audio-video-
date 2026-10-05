"""On-demand screen frames, duplicate suppression, and usage attribution.

Streaming frames at 1 fps grew the model's context by roughly 390 tokens per
second, which is what made late turns take 10-30s. Frames are now sent when
they carry information — after a computer-use action, when the user starts
speaking, and on a slow heartbeat — and an unchanged screen sends nothing.

The per-modality token counts that would have shown this directly were being
silently zeroed (``str(MediaModality.VIDEO)`` is ``"MediaModality.VIDEO"``,
never ``"VIDEO"``), so they are covered here too.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from audit.journal import Journal
from core.commands import Disconnect, SetShareScreen
from core.events import ConnectionState, ConnectionStateChanged, UsageUpdate
from core.session import LiveSession, _modality_name
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
        share_screen=True,
        computer_use_enabled=True,
    )


def _make_session(
    settings: AppSettings, tmp_path: Path
) -> tuple[LiveSession, asyncio.Queue[Any], asyncio.Queue[Any], Journal]:
    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    event_queue: asyncio.Queue[Any] = asyncio.Queue()
    journal = Journal(run_id="frames", log_dir=tmp_path)
    session = LiveSession(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=Store(db_path=tmp_path / "state.db"),
        run_id="frames",
    )
    return session, command_queue, event_queue, journal


def _patch_common(monkeypatch: Any, live: FakeLive) -> None:
    monkeypatch.setattr("google.genai.Client", make_fake_client_factory(live))
    monkeypatch.setattr("core.session.get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr("media.devices.list_input_devices", lambda: [])
    monkeypatch.setattr("media.devices.list_output_devices", lambda: [])
    monkeypatch.setattr("core.session.capture_jpeg", lambda *a, **k: b"\xff\xd8frame")


async def _wait_live(event_queue: asyncio.Queue[Any]) -> None:
    while True:
        event = await asyncio.wait_for(event_queue.get(), timeout=5)
        if (
            isinstance(event, ConnectionStateChanged)
            and event.state == ConnectionState.LIVE
        ):
            return


class _FakeSocket:
    """Minimal stand-in capturing realtime video sends."""

    def __init__(self) -> None:
        self.videos: list[bytes] = []

    async def send_realtime_input(self, **kwargs: Any) -> None:
        video = kwargs.get("video")
        if video is not None:
            self.videos.append(video.data)


# ---------------------------------------------------------------------------
# Usage attribution
# ---------------------------------------------------------------------------


def test_modality_name_handles_enum_and_string() -> None:
    assert _modality_name(types.MediaModality.VIDEO) == "VIDEO"
    assert _modality_name(types.MediaModality.AUDIO) == "AUDIO"
    assert _modality_name("VIDEO") == "VIDEO"
    assert _modality_name("MediaModality.TEXT") == "TEXT"


@pytest.mark.asyncio
async def test_usage_update_reports_audio_and_video_tokens(
    settings: AppSettings, tmp_path: Path, monkeypatch: Any
) -> None:
    """Per-modality counts must survive the enum, not silently read zero."""
    live = FakeLive()
    _patch_common(monkeypatch, live)
    session, command_queue, event_queue, journal = _make_session(settings, tmp_path)

    runner = asyncio.create_task(session.run())
    try:
        await _wait_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        fake_session.usage(
            prompt_token_count=1500,
            response_token_count=200,
            total_token_count=1700,
            prompt_tokens_details=[
                # Screen frames are billed as IMAGE, not VIDEO.
                types.ModalityTokenCount(
                    modality=types.MediaModality.IMAGE, token_count=700
                ),
                types.ModalityTokenCount(
                    modality=types.MediaModality.VIDEO, token_count=200
                ),
                types.ModalityTokenCount(
                    modality=types.MediaModality.AUDIO, token_count=400
                ),
            ],
            response_tokens_details=[
                types.ModalityTokenCount(
                    modality=types.MediaModality.AUDIO, token_count=180
                ),
            ],
        )

        usage: UsageUpdate | None = None
        while usage is None:
            event = await asyncio.wait_for(event_queue.get(), timeout=5)
            if isinstance(event, UsageUpdate):
                usage = event
    finally:
        await command_queue.put(Disconnect())
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(runner, timeout=5)
        journal.close()

    assert usage.video_tokens == 900
    assert usage.audio_tokens == 580  # 400 prompt + 180 response


# ---------------------------------------------------------------------------
# Connect config
# ---------------------------------------------------------------------------


def test_connect_config_defaults_leave_model_input_untouched(
    settings: AppSettings,
) -> None:
    """Defaults must not change what the model sees.

    ``only_activity`` drops input that arrived between turns — where every
    post-action frame lives — and a compression trigger truncates a task's
    earlier steps out from under it. Both are opt-in.
    """
    config = settings.as_connect_config(tools=[])

    compression = config.context_window_compression
    assert compression is not None
    assert compression.trigger_tokens is None
    assert compression.sliding_window is not None
    assert compression.sliding_window.target_tokens == settings.target_tokens

    realtime = config.realtime_input_config
    assert realtime is not None
    assert realtime.turn_coverage is None
    assert settings.frame_mode == "stream"


def test_connect_config_applies_opted_in_context_limits(
    settings: AppSettings,
) -> None:
    settings.turn_coverage = "only_activity"
    settings.trigger_tokens = 32000
    config = settings.as_connect_config(tools=[])

    assert config.context_window_compression is not None
    assert config.context_window_compression.trigger_tokens == 32000
    assert config.realtime_input_config is not None
    assert (
        config.realtime_input_config.turn_coverage
        == types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY
    )


def test_frame_mode_rejects_unknown_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid frame_mode"):
        AppSettings(
            working_dir=tmp_path, workspace_root=tmp_path, frame_mode="continuous"
        )


def test_turn_coverage_rejects_unknown_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid turn_coverage"):
        AppSettings(
            working_dir=tmp_path, workspace_root=tmp_path, turn_coverage="everything"
        )


# ---------------------------------------------------------------------------
# Frame gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_frame_drops_unchanged_screen(
    settings: AppSettings, tmp_path: Path, monkeypatch: Any
) -> None:
    settings.frame_min_interval_seconds = 0.0
    monkeypatch.setattr("core.session.capture_jpeg", lambda *a, **k: b"same-frame")

    session, _, _, _ = _make_session(settings, tmp_path)
    socket = _FakeSocket()

    assert await session._send_frame(socket, reason="first") is True
    assert await session._send_frame(socket, reason="second") is False
    assert socket.videos == [b"same-frame"]


@pytest.mark.asyncio
async def test_send_frame_sends_when_screen_changes(
    settings: AppSettings, tmp_path: Path, monkeypatch: Any
) -> None:
    settings.frame_min_interval_seconds = 0.0
    frames = iter([b"frame-a", b"frame-b"])
    monkeypatch.setattr("core.session.capture_jpeg", lambda *a, **k: next(frames))

    session, _, _, _ = _make_session(settings, tmp_path)
    socket = _FakeSocket()

    assert await session._send_frame(socket, reason="first") is True
    assert await session._send_frame(socket, reason="second") is True
    assert socket.videos == [b"frame-a", b"frame-b"]


@pytest.mark.asyncio
async def test_send_frame_honours_minimum_interval(
    settings: AppSettings, tmp_path: Path, monkeypatch: Any
) -> None:
    settings.frame_min_interval_seconds = 60.0
    frames = iter([b"frame-a", b"frame-b"])
    monkeypatch.setattr("core.session.capture_jpeg", lambda *a, **k: next(frames))

    session, _, _, _ = _make_session(settings, tmp_path)
    socket = _FakeSocket()

    assert await session._send_frame(socket, reason="first") is True
    assert await session._send_frame(socket, reason="too-soon") is False
    assert socket.videos == [b"frame-a"]


@pytest.mark.asyncio
async def test_post_action_frame_forces_send_of_identical_screen(
    settings: AppSettings, tmp_path: Path, monkeypatch: Any
) -> None:
    """A no-op action must still be shown, or the model cannot see it failed."""
    settings.frame_min_interval_seconds = 0.0
    monkeypatch.setattr("core.session.capture_jpeg", lambda *a, **k: b"same-frame")

    session, _, _, _ = _make_session(settings, tmp_path)
    socket = _FakeSocket()

    await session._send_frame(socket, reason="baseline")
    await session._push_fresh_frame(socket)

    assert socket.videos == [b"same-frame", b"same-frame"]


@pytest.mark.asyncio
async def test_post_action_frame_ignores_the_interval_floor(
    settings: AppSettings, tmp_path: Path, monkeypatch: Any
) -> None:
    """A heartbeat firing just before an action must not swallow its frame."""
    settings.frame_min_interval_seconds = 60.0
    frames = iter([b"before-action", b"after-action"])
    monkeypatch.setattr("core.session.capture_jpeg", lambda *a, **k: next(frames))

    session, _, _, _ = _make_session(settings, tmp_path)
    socket = _FakeSocket()

    await session._send_frame(socket, reason="heartbeat")
    await session._push_fresh_frame(socket)

    assert socket.videos == [b"before-action", b"after-action"]


@pytest.mark.asyncio
async def test_on_demand_mode_starts_no_capture_thread(
    settings: AppSettings, tmp_path: Path, monkeypatch: Any
) -> None:
    """The 1 fps capture thread is what on-demand mode exists to avoid."""
    settings.frame_mode = "on_demand"
    live = FakeLive()
    _patch_common(monkeypatch, live)

    constructed = False

    class _TrackingCapture:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr("core.session.ScreenCapture", _TrackingCapture)

    session, command_queue, event_queue, journal = _make_session(settings, tmp_path)
    runner = asyncio.create_task(session.run())
    try:
        await _wait_live(event_queue)
        await asyncio.sleep(0.05)
    finally:
        await command_queue.put(Disconnect())
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(runner, timeout=5)
        journal.close()

    assert constructed is False


@pytest.mark.asyncio
async def test_set_share_screen_monitor_is_used_for_on_demand_frames(
    settings: AppSettings, tmp_path: Path, monkeypatch: Any
) -> None:
    settings.share_screen = False
    settings.computer_use_enabled = False
    settings.frame_mode = "on_demand"
    settings.frame_heartbeat_seconds = 0.01
    settings.frame_min_interval_seconds = 0.0
    live = FakeLive()
    _patch_common(monkeypatch, live)

    captured_monitors: list[int] = []
    selected_monitors: list[int] = []
    monkeypatch.setattr("core.session.validate_monitor", lambda _monitor: None)
    monkeypatch.setattr(
        "core.session.computer_tools.set_monitor", selected_monitors.append
    )

    def capture(monitor: int = 1, **_kwargs: Any) -> bytes:
        captured_monitors.append(monitor)
        return b"frame"

    monkeypatch.setattr(
        "core.session.capture_jpeg", capture
    )

    session, command_queue, event_queue, journal = _make_session(settings, tmp_path)
    await command_queue.put(SetShareScreen(enabled=True, monitor=2))
    runner = asyncio.create_task(session.run())
    try:
        await _wait_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        sent_frame = False
        for _ in range(100):
            outgoing = fake_session.pop_outbox()
            if any(
                isinstance(message, types.LiveClientRealtimeInput)
                and message.video is not None
                for message in outgoing
            ):
                sent_frame = True
                break
            await asyncio.sleep(0.01)

        assert sent_frame
        assert captured_monitors[0] == 2
        assert selected_monitors[-1] == 2
    finally:
        await command_queue.put(Disconnect())
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(runner, timeout=5)
        journal.close()
