"""Audio timing and backlog regressions without PortAudio devices."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from media.microphone import Microphone
from media.speaker import Speaker


class FakeStream:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class DelayedLoop:
    """Hold callback delivery to simulate a stalled event loop."""

    def __init__(self) -> None:
        self.pending: list[tuple[Any, tuple[Any, ...]]] = []

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
        self.pending.append((callback, args))

    def deliver(self) -> None:
        pending, self.pending = self.pending, []
        for callback, args in pending:
            callback(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [20, 40, 100])
async def test_mic_chunk_size_and_capacity_follow_duration(
    monkeypatch: Any, duration: int
) -> None:
    streams: list[FakeStream] = []

    def factory(**kwargs: Any) -> FakeStream:
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr("media.microphone.sd.RawInputStream", factory)
    mic = Microphone(3, chunk_duration_ms=duration)
    await mic.start()
    assert streams[0].kwargs["blocksize"] == 16 * duration
    assert streams[0].kwargs["device"] == 3
    loop = DelayedLoop()
    mic._loop = loop  # type: ignore[assignment]
    for _ in range(400 // duration + 3):
        mic._audio_callback(b"\x01\x00" * (16 * duration), 16 * duration, None, None)
    snapshot = mic.diagnostics
    assert snapshot["queued_ms"] == 400.0
    assert snapshot["dropped_chunks"] == 3.0
    assert snapshot["chunk_duration_ms"] == float(duration)
    assert len(loop.pending) == 1  # bridge itself is bounded during loop stalls
    loop.deliver()
    iterator = mic.chunks()
    assert await anext(iterator) == b"\x01\x00" * (16 * duration)
    assert mic.diagnostics["queued_ms"] == 400.0 - duration
    await iterator.aclose()
    await mic.stop()


def test_mic_default_and_invalid_durations() -> None:
    assert Microphone().diagnostics["chunk_duration_ms"] == 100.0
    for duration in [0, 10, 30, 200]:
        with pytest.raises(ValueError, match="20, 40, or 100"):
            Microphone(chunk_duration_ms=duration)


@pytest.mark.asyncio
async def test_mic_age_includes_callback_scheduling_delay(monkeypatch: Any) -> None:
    monkeypatch.setattr("media.microphone.sd.RawInputStream", FakeStream)
    clock = [10.0]
    monkeypatch.setattr("media.microphone.time.monotonic", lambda: clock[0])
    mic = Microphone()
    await mic.start()
    loop = DelayedLoop()
    mic._loop = loop  # type: ignore[assignment]
    old = b"\x01\x00" * 1600
    fresh = b"\x02\x00" * 1600
    mic._audio_callback(old, 1600, None, None)
    clock[0] = 10.25
    assert mic.diagnostics["oldest_chunk_ms"] == pytest.approx(250)
    clock[0] = 10.5
    mic._audio_callback(fresh, 1600, None, "overflow")
    loop.deliver()
    iterator = mic.chunks()
    assert await anext(iterator) == fresh
    assert mic.diagnostics["dropped_chunks"] == 1.0
    assert mic.diagnostics["callback_status_count"] == 1.0
    assert mic.diagnostics["queued_ms"] == 0.0
    assert mic.diagnostics["oldest_chunk_ms"] == 0.0
    await iterator.aclose()
    await mic.stop()


@pytest.mark.asyncio
async def test_mic_does_not_yield_audio_that_aged_while_waiting(monkeypatch: Any) -> None:
    monkeypatch.setattr("media.microphone.sd.RawInputStream", FakeStream)
    clock = [10.0]
    monkeypatch.setattr("media.microphone.time.monotonic", lambda: clock[0])
    mic = Microphone()
    await mic.start()
    mic._audio_callback(b"\x01\x00" * 1600, 1600, None, None)
    await asyncio.sleep(0)
    clock[0] = 10.5
    iterator = mic.chunks()
    consumer = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    assert not consumer.done()
    await mic.stop()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(consumer, 1)
    assert mic.diagnostics["dropped_chunks"] == 1.0


@pytest.mark.asyncio
async def test_mic_gate_keeps_level_updates_without_queuing_or_counting_loss(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("media.microphone.sd.RawInputStream", FakeStream)
    mic = Microphone()
    await mic.start()
    mic.set_gate(False)
    mic._audio_callback(b"\xff\x3f" * 1600, 1600, None, None)
    await asyncio.sleep(0)
    assert mic.level > 0.4
    assert mic.diagnostics["queued_ms"] == 0.0
    assert mic.diagnostics["dropped_chunks"] == 0.0
    iterator = mic.chunks()
    consumer = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    await mic.stop()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(consumer, 1)


@pytest.mark.asyncio
async def test_mic_immediate_restart_ignores_previous_stop_notification(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("media.microphone.sd.RawInputStream", FakeStream)
    mic = Microphone()
    await mic.start()
    await mic.stop()
    await mic.start()
    await asyncio.sleep(0)
    fresh = b"\x02\x00" * 1600
    mic._audio_callback(fresh, 1600, None, None)
    iterator = mic.chunks()
    try:
        assert await asyncio.wait_for(anext(iterator), 1) == fresh
    finally:
        await iterator.aclose()
        await mic.stop()


@pytest.mark.asyncio
async def test_mic_restart_ends_previous_waiter_without_consuming_new_audio(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("media.microphone.sd.RawInputStream", FakeStream)
    mic = Microphone()
    await mic.start()
    old_iterator = mic.chunks()
    old_waiter = asyncio.create_task(anext(old_iterator))
    await asyncio.sleep(0)
    await mic.stop()
    await mic.start()
    fresh = b"\x03\x00" * 1600
    mic._audio_callback(fresh, 1600, None, None)
    new_iterator = mic.chunks()
    try:
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(old_waiter, 1)
        assert await asyncio.wait_for(anext(new_iterator), 1) == fresh
    finally:
        await old_iterator.aclose()
        await new_iterator.aclose()
        await mic.stop()


@pytest.mark.asyncio
async def test_mic_mute_discards_unsent_audio_before_immediate_unmute(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("media.microphone.sd.RawInputStream", FakeStream)
    mic = Microphone()
    await mic.start()
    mic._audio_callback(b"\x01\x00" * 1600, 1600, None, None)
    mic.set_gate(False)
    mic.set_gate(True)
    fresh = b"\x04\x00" * 1600
    mic._audio_callback(fresh, 1600, None, None)
    iterator = mic.chunks()
    try:
        assert await asyncio.wait_for(anext(iterator), 1) == fresh
        assert mic.diagnostics["queued_ms"] == 0.0
        assert mic.diagnostics["dropped_chunks"] == 0.0
    finally:
        await iterator.aclose()
        await mic.stop()


def _pull(speaker: Speaker, frames: int, status: Any = None) -> bytes:
    output = bytearray(frames * 2)
    speaker._audio_callback(output, frames, None, status)
    return bytes(output)


def test_speaker_diagnostics_include_pending_and_only_count_pcm_as_played() -> None:
    speaker = Speaker(stream_factory=FakeStream)
    speaker._active = True
    pcm = b"\x03\x00" * 2400  # 100 ms
    speaker.write(pcm)
    assert speaker.diagnostics["queued_ms"] == 100.0
    assert _pull(speaker, 600) == pcm[:1200]
    assert speaker.diagnostics["queued_ms"] == 75.0
    assert speaker.diagnostics["played_ms"] == 25.0
    speaker.write(pcm)
    assert speaker.diagnostics["queued_ms"] == 175.0
    assert speaker.diagnostics["high_water_ms"] == 175.0
    _pull(speaker, 4800, "underflow")
    snapshot = speaker.diagnostics
    assert snapshot["queued_ms"] == 0.0
    assert snapshot["played_ms"] == 200.0
    assert snapshot["underruns"] == 1.0
    assert snapshot["callback_status_count"] == 1.0
    snapshot["played_ms"] = -1
    assert speaker.diagnostics["played_ms"] == 200.0


def test_speaker_flush_resets_backlog_but_preserves_cumulative_counters() -> None:
    speaker = Speaker(stream_factory=FakeStream)
    speaker._active = True
    speaker.write(b"\x01\x00" * 2400)
    _pull(speaker, 240)
    speaker.write(b"\x02\x00" * 2400)
    speaker.flush()
    assert speaker.diagnostics["queued_ms"] == 0.0
    assert speaker.diagnostics["played_ms"] == 10.0
    assert speaker.diagnostics["high_water_ms"] == 190.0
    assert _pull(speaker, 240) == bytes(480)
    speaker.write(b"\x04\x00" * 240)
    assert _pull(speaker, 240) == b"\x04\x00" * 240
    assert speaker.diagnostics["queued_ms"] == 0.0


def test_speaker_backlog_is_observed_without_truncating_generated_speech() -> None:
    speaker = Speaker(stream_factory=FakeStream)
    speaker._active = True
    pcm = b"\x05\x00" * 24000
    for _ in range(30):
        speaker.write(pcm)
    assert speaker.diagnostics["queued_ms"] == 30000.0
    for _ in range(30):
        assert _pull(speaker, 24000) == pcm
    assert speaker.diagnostics["played_ms"] == 30000.0
    assert speaker.diagnostics["underruns"] == 0.0


def test_speaker_concurrent_writes_callbacks_and_snapshots_preserve_audio() -> None:
    speaker = Speaker(stream_factory=FakeStream)
    speaker._active = True
    finished = threading.Event()
    barrier = threading.Barrier(2)
    pcm = b"\x06\x01" * 240
    received: list[bytes] = []

    def produce() -> None:
        barrier.wait(timeout=2)
        try:
            for _ in range(200):
                speaker.write(pcm)
                time.sleep(0)
        finally:
            finished.set()

    thread = threading.Thread(target=produce)
    thread.start()
    barrier.wait(timeout=2)
    try:
        while not finished.is_set() or speaker.diagnostics["queued_ms"]:
            output = _pull(speaker, 480)
            # Remove only callback padding; every generated sample is nonzero.
            received.append(output.rstrip(b"\x00"))
            snapshot = speaker.diagnostics
            assert snapshot["queued_ms"] >= 0.0
            assert snapshot["high_water_ms"] >= snapshot["queued_ms"]
            time.sleep(0)
    finally:
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert b"".join(received) == pcm * 200
    assert speaker.diagnostics["played_ms"] == 2000.0
    assert speaker.diagnostics["queued_ms"] == 0.0


@pytest.mark.asyncio
async def test_speaker_stop_restart_discards_old_pending_and_queued_audio() -> None:
    speaker = Speaker(stream_factory=FakeStream)
    await speaker.start()
    speaker.write(b"\x01\x00" * 2400)
    _pull(speaker, 240)
    speaker.write(b"\x02\x00" * 2400)
    await speaker.stop()
    assert not speaker.is_playing
    await speaker.start()
    try:
        assert _pull(speaker, 240) == bytes(480)
        assert speaker.diagnostics["queued_ms"] == 0.0
        assert speaker.diagnostics["played_ms"] == 10.0
        assert speaker.diagnostics["high_water_ms"] == 190.0
        speaker.write(b"\x03\x00" * 240)
        assert _pull(speaker, 240) == b"\x03\x00" * 240
    finally:
        await speaker.stop()


@pytest.mark.asyncio
async def test_speaker_write_racing_with_stop_cannot_enqueue_for_next_start(
    monkeypatch: Any,
) -> None:
    speaker = Speaker(stream_factory=FakeStream)
    await speaker.start()
    entered = threading.Event()
    release = threading.Event()

    def pause_level_update(_chunk: bytes) -> None:
        entered.set()
        assert release.wait(timeout=2)

    monkeypatch.setattr(speaker, "_update_level", pause_level_update)
    writer = threading.Thread(target=speaker.write, args=(b"\x05\x00" * 240,))
    writer.start()
    try:
        assert entered.wait(timeout=2)
        await speaker.stop()
        # Keep the old writer paused through restart, when active is true again.
        await speaker.start()
        release.set()
        writer.join(timeout=2)
        assert not writer.is_alive()
        assert _pull(speaker, 240) == bytes(480)
        assert speaker.diagnostics["queued_ms"] == 0.0
        assert speaker.diagnostics["high_water_ms"] == 0.0
    finally:
        release.set()
        writer.join(timeout=2)
        await speaker.stop()
