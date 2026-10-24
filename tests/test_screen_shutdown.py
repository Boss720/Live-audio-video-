"""Capture lifecycle regressions using fake threads, without screen access."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from media.screen import ScreenCapture


@pytest.mark.asyncio
async def test_screen_stop_keeps_event_loop_responsive() -> None:
    capture = ScreenCapture()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    loop_progressed_during_join: list[bool] = []

    class JoinProbe:
        def join(self, timeout: float) -> None:
            loop.call_soon_threadsafe(release.set)
            loop_progressed_during_join.append(release.wait(timeout=0.3))

        def is_alive(self) -> bool:
            return False

    capture._loop = loop
    capture._running = True
    capture._thread = cast(threading.Thread, JoinProbe())

    await capture.stop()
    assert loop_progressed_during_join == [True]
    assert capture._thread is None


@pytest.mark.asyncio
async def test_screen_stop_retains_thread_that_did_not_exit() -> None:
    capture = ScreenCapture()

    class StalledThread:
        alive = True

        def join(self, timeout: float) -> None:
            pass

        def is_alive(self) -> bool:
            return self.alive

    thread = StalledThread()
    capture._running = True
    capture._thread = cast(threading.Thread, thread)

    await capture.stop()
    assert capture._thread is thread
    assert not capture._running
    with pytest.raises(RuntimeError, match="still stopping"):
        await capture.start()

    thread.alive = False
    await capture.stop()
    assert capture._thread is None


@pytest.mark.asyncio
async def test_late_frame_cannot_replace_shutdown_sentinel() -> None:
    capture = ScreenCapture()
    capture._loop = asyncio.get_running_loop()
    capture._running = True

    await capture.stop()
    capture._publish(b"late frame from a capture already in progress")
    await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError, match="Screen capture stopped"):
        await capture.read_frame()


@pytest.mark.asyncio
async def test_monitor_validation_finishing_after_stop_preserves_sentinel(
    monkeypatch: Any,
) -> None:
    capture = ScreenCapture()
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    capture._loop = loop
    capture._running = True

    def validate(monitor: int) -> dict[str, int]:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3)
        return {"width": 1, "height": 1}

    monkeypatch.setattr("media.screen.validate_monitor", validate)
    switching = asyncio.create_task(capture.set_monitor(2))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await capture.stop()
        release.set()
        await asyncio.wait_for(switching, timeout=1)
        with pytest.raises(asyncio.CancelledError, match="Screen capture stopped"):
            await asyncio.wait_for(capture.read_frame(), timeout=0.2)
    finally:
        release.set()
        await asyncio.gather(switching, return_exceptions=True)
        await capture.stop()


@pytest.mark.asyncio
async def test_low_fps_capture_wakes_for_stop_and_can_restart(monkeypatch: Any) -> None:
    region = {"left": 0, "top": 0, "width": 1, "height": 1}

    class FakeMSS:
        monitors = [region, region]

        def __enter__(self) -> FakeMSS:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def grab(self, region: Any) -> SimpleNamespace:
            return SimpleNamespace(size=(1, 1), bgra=bytes(4))

    monkeypatch.setattr("media.screen._mss_factory", lambda _mss: FakeMSS)
    capture = ScreenCapture(fps=0.05)
    for _ in range(2):
        try:
            await capture.start()
            frame = await asyncio.wait_for(capture.read_frame(), timeout=2)
            assert frame.startswith(b"\xff\xd8")
            await asyncio.wait_for(capture.stop(), timeout=0.5)
            assert capture._thread is None
        finally:
            await capture.stop()
