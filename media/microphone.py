"""Microphone capture for the Gemini Live Agent."""

from __future__ import annotations

import array
import asyncio
import math
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

import sounddevice as sd

from media import devices
from obs.logging_setup import get_logger

logger = get_logger(__name__)

INPUT_SAMPLE_RATE = 16_000  # Hz
CHANNELS = 1
DTYPE = "int16"
CHUNK_DURATION_MS = 40
INPUT_CHUNK_SAMPLES = int(INPUT_SAMPLE_RATE * CHUNK_DURATION_MS / 1000)
# Backlog bound in milliseconds, independent of the chunk size.
MAX_QUEUE_MS = 400


class Microphone:
    """Captures audio from a microphone as 16 kHz 16-bit mono PCM chunks.

    Captured chunks are bridged from the PortAudio callback thread to the
    async event loop via a bounded deque and coalesced wakeups. Capture age
    includes event-loop scheduling delays. When the gate is closed, chunks
    are discarded instead of queued.
    """

    def __init__(
        self,
        device: str | int | None = None,
        *,
        chunk_duration_ms: int = CHUNK_DURATION_MS,
        echo_gate: Callable[[], bool] | None = None,
    ) -> None:
        if chunk_duration_ms not in (20, 40, 100):
            raise ValueError("chunk_duration_ms must be 20, 40, or 100")
        if isinstance(device, str):
            self._device_index = devices.validate_device(device, "input")
        else:
            self._device_index = device

        self._chunk_duration_ms = chunk_duration_ms
        self._chunk_samples = INPUT_SAMPLE_RATE * chunk_duration_ms // 1000
        self._queue_capacity = MAX_QUEUE_MS // chunk_duration_ms
        self._queue: deque[tuple[float, bytes] | None] = deque()
        self._queue_lock = threading.Lock()
        self._ready = asyncio.Event()
        self._wake_pending = False
        self._level_chunk = b""
        self._dropped_chunks = 0
        self._callback_status_count = 0
        self._generation = 0
        self._stream: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._gate_open = True
        # Echo gate: returns True while mic audio must be suppressed (speaker
        # is playing). Suppressed chunks are sent as silence, not dropped, so
        # the stream stays continuous for server VAD. Tradeoff: with the gate
        # active the user cannot barge in by voice on speakers; headsets
        # bypass it. Real AEC would replace this.
        self._echo_gate = echo_gate
        self._suppressed_chunks = 0

        self._last_level = 0.0
        self._level_lock = threading.Lock()

    # -- Public API --

    async def start(self) -> None:
        """Start capturing audio from the selected microphone."""
        self._loop = asyncio.get_running_loop()
        with self._queue_lock:
            self._generation += 1
            self._ready = asyncio.Event()
            self._wake_pending = False
        self._drain_queue()
        with self._queue_lock:
            self._running = True

        self._stream = sd.RawInputStream(
            samplerate=INPUT_SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=self._chunk_samples,
            device=self._device_index,
            latency="low",  # PortAudio's default is the high-latency profile
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.info("[MIC] Microphone active")

    async def stop(self) -> None:
        """Stop capturing and release the microphone."""
        with self._queue_lock:
            self._running = False
            generation, ready = self._generation, self._ready

        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Mic stop failed: %s", exc)
            try:
                self._stream.close()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Mic close failed: %s", exc)
            self._stream = None

        # Release any waiter in chunks().
        if self._loop is not None and not self._loop.is_closed():
            if self._loop is asyncio.get_running_loop():
                self._enqueue_sentinel(generation, ready)
            else:
                self._loop.call_soon_threadsafe(self._enqueue_sentinel, generation, ready)
        logger.info("[MIC] Microphone stopped")

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield captured PCM chunks until the microphone is stopped."""
        with self._queue_lock:
            generation, ready = self._generation, self._ready
        while True:
            await ready.wait()
            with self._queue_lock:
                if generation != self._generation:
                    break
                self._expire_chunks(time.monotonic())
                if not self._queue:
                    ready.clear()
                    continue
                item = self._queue.popleft()
                if not self._queue:
                    ready.clear()
            if item is None:
                break
            yield item[1]

    def set_gate(self, open: bool) -> None:  # noqa: A002 - matches domain language
        """Open or close the audio gate.

        Closing also discards unsent queued PCM. Intentional mute discards
        are excluded from the capacity/age ``dropped_chunks`` counter.
        """
        with self._queue_lock:
            self._gate_open = open
            if not open:
                self._queue = deque(item for item in self._queue if item is None)

    @property
    def gate_open(self) -> bool:
        """Return whether the audio gate is currently open."""
        with self._queue_lock:
            return self._gate_open

    @property
    def diagnostics(self) -> dict[str, float]:
        """Return a coherent numeric snapshot; counters last for this instance.

        Age starts at callback entry (not the hardware ADC timestamp). The
        400 ms age limit is enforced on capture and consumption; a snapshot
        may report older queued audio while neither operation is running.
        """
        now = time.monotonic()
        with self._queue_lock:
            chunks = [item for item in self._queue if item is not None]
            return {
                "queued_ms": float(len(chunks) * self._chunk_duration_ms),
                "oldest_chunk_ms": max(0.0, (now - chunks[0][0]) * 1000) if chunks else 0.0,
                "dropped_chunks": float(self._dropped_chunks),
                "callback_status_count": float(self._callback_status_count),
                "echo_suppressed_chunks": float(self._suppressed_chunks),
                "chunk_duration_ms": float(self._chunk_duration_ms),
            }

    @property
    def level(self) -> float:
        """Return the most recent input level, normalised to ``[0.0, 1.0]``."""
        with self._level_lock:
            return self._last_level

    # -- PortAudio callback (runs on the audio thread) --

    def _audio_callback(
        self, indata: Any, frames: int, time_info: Any, status: Any
    ) -> None:
        captured_at = time.monotonic()
        if status:
            with self._queue_lock:
                self._callback_status_count += 1
        chunk = bytes(indata)
        self._enqueue_chunk(chunk, captured_at)

    # -- Queue bridging --

    def _enqueue_chunk(self, chunk: bytes, captured_at: float | None = None) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if captured_at is None:
            captured_at = time.monotonic()
        suppress = False
        if self._echo_gate is not None:
            try:
                suppress = bool(self._echo_gate())
            except Exception:  # pragma: no cover - never break capture
                suppress = False
        if suppress:
            chunk = bytes(len(chunk))
        with self._queue_lock:
            if not self._running:
                return
            if suppress:
                self._suppressed_chunks += 1
            self._level_chunk = chunk
            self._expire_chunks(captured_at)
            if self._gate_open:
                if len(self._queue) >= self._queue_capacity:
                    self._queue.popleft()
                    self._dropped_chunks += 1
                self._queue.append((captured_at, chunk))
            if self._wake_pending:
                return
            self._wake_pending = True
            generation = self._generation
        try:
            loop.call_soon_threadsafe(self._notify_chunks, generation)
        except RuntimeError:  # pragma: no cover - event loop closed during shutdown
            with self._queue_lock:
                if generation == self._generation:
                    self._wake_pending = False

    def _notify_chunks(self, generation: int) -> None:
        """Run RMS work on the event loop, never on the capture callback."""
        with self._queue_lock:
            if generation != self._generation:
                return
            self._wake_pending = False
            chunk, self._level_chunk = self._level_chunk, b""
            queued = bool(self._queue)
        self._update_level(chunk)
        if queued:
            self._ready.set()

    def _expire_chunks(self, now: float) -> None:
        """Drop stale capture under the queue lock; at most 20 iterations."""
        while self._queue:
            oldest = self._queue[0]
            if oldest is None or (now - oldest[0]) * 1000 <= MAX_QUEUE_MS:
                break
            self._queue.popleft()
            self._dropped_chunks += 1

    def _enqueue_sentinel(self, generation: int, ready: asyncio.Event) -> None:
        """Put a sentinel value to unblock chunk consumers."""
        with self._queue_lock:
            if generation == self._generation:
                if len(self._queue) >= self._queue_capacity:
                    self._queue.popleft()
                    self._dropped_chunks += 1
                self._queue.append(None)
        ready.set()

    def _drain_queue(self) -> None:
        """Drop any stale chunks left over from a previous session."""
        with self._queue_lock:
            self._queue.clear()
            self._level_chunk = b""
        self._ready.clear()

    def _update_level(self, chunk: bytes) -> None:
        if not chunk:
            return
        try:
            samples = array.array("h", chunk)
        except ValueError:
            return
        n = len(samples)
        if n == 0:
            return
        mean_square = sum(sample * sample for sample in samples) / n
        rms = math.sqrt(mean_square)
        with self._level_lock:
            self._last_level = min(rms / 32768.0, 1.0)
