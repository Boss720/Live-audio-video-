"""Speaker playback for the Gemini Live Agent."""

from __future__ import annotations

import array
import math
import queue
import threading
import time
from typing import Any

import sounddevice as sd

from media import devices
from obs.logging_setup import get_logger

logger = get_logger(__name__)

OUTPUT_SAMPLE_RATE = 24_000  # Hz
CHANNELS = 1
DTYPE = "int16"
_BYTES_PER_FRAME = CHANNELS * 2  # int16
# Fixed callback period: 20 ms at 24 kHz (Gemini output rate).
BLOCKSIZE_FRAMES = OUTPUT_SAMPLE_RATE // 50
# Audio queued before playback starts at stream start, after a flush, or
# after an underrun; absorbs network jitter so speech does not stutter.
PREBUFFER_MS = 40
_PREBUFFER_BYTES = OUTPUT_SAMPLE_RATE * PREBUFFER_MS // 1000 * _BYTES_PER_FRAME
# Echo gating: output counts as "playing" for this long after the last
# non-silent block, covering the room reverb tail.
ECHO_HANGOVER_MS = 250
# Blocks quieter than this normalised RMS (~-60 dBFS) count as silence.
_AUDIBLE_LEVEL = 0.001


class Speaker:
    """Plays 24 kHz 16-bit mono PCM audio through the selected speaker.

    The stream is a *callback* (pull) stream: PortAudio calls
    ``_audio_callback`` on its own real-time thread whenever it needs more
    frames, and the callback drains a queue of PCM chunks. There is no writer
    thread and no blocking ``stream.write()`` call anywhere.

    That design is what makes barge-in both instant and safe: ``flush()`` only
    bumps a generation counter and drops queued audio, so playback goes silent
    within one callback period. The stream itself is never aborted, closed, or
    recreated mid-conversation, which avoids the Windows MME errors ("wave
    header not prepared", "media data is still playing") and the native
    crash that tearing a stream down mid-write could cause.

    Thread-safety: queue, generation, pending audio, and diagnostic counters
    share a short lock. The callback performs no logging, RMS calculation,
    or I/O while holding it. Flush swaps the queue rather than walking the
    backlog under the lock. Audio already handed to PortAudio can play for
    one callback period after a flush.
    """

    def __init__(
        self,
        device: str | int | None = None,
        *,
        stream_factory: Any = sd.RawOutputStream,
    ) -> None:
        if isinstance(device, str):
            self._device_index = devices.validate_device(device, "output")
        else:
            self._device_index = device

        self._stream_factory = stream_factory
        self._stream: Any | None = None
        self._active = False
        self._queue: queue.Queue[tuple[int, bytes]] = queue.Queue()
        self._lock = threading.Lock()
        self._generation = 0
        self._pending = b""  # unconsumed remainder of the current chunk
        self._queued_bytes = 0  # includes the pending remainder
        self._high_water_bytes = 0
        self._played_bytes = 0
        self._underruns = 0
        self._callback_status_count = 0
        # True until prebuffer bytes are queued; re-armed by flush/underrun.
        self._prebuffering = True
        self._prebuffer_wait_bytes = 0  # silence emitted while partly buffered

        self._last_level = 0.0
        self._level_lock = threading.Lock()
        self._last_audible_at: float | None = None  # monotonic; under _level_lock

    # -- Public API --

    async def start(self) -> None:
        """Open the speaker output stream; PortAudio pulls audio via callback."""
        self._stream = self._stream_factory(
            samplerate=OUTPUT_SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            device=self._device_index,
            blocksize=BLOCKSIZE_FRAMES,
            latency="low",
            callback=self._audio_callback,
        )
        self._stream.start()
        with self._lock:
            self._active = True
        logger.info("[SPK] Speaker active")

    async def stop(self) -> None:
        """Stop, discard unsent PCM, and close the speaker output stream.

        Lifetime diagnostic counters are preserved across stop/start.
        """
        self._discard_backlog(deactivate=True)
        if self._stream is None:
            return

        try:
            self._stream.stop()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Speaker stop failed: %s", exc)
        try:
            self._stream.close()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Speaker close failed: %s", exc)
        self._stream = None
        logger.info("[SPK] Speaker stopped")

    def write(self, pcm_bytes: bytes) -> None:
        """Queue PCM audio for playback. Never blocks the async loop."""
        if not pcm_bytes:
            return
        with self._lock:
            if not self._active:
                return
            self._queue.put_nowait((self._generation, pcm_bytes))
            self._queued_bytes += len(pcm_bytes)
            self._high_water_bytes = max(self._high_water_bytes, self._queued_bytes)

    def flush(self) -> None:
        """Drop all pending audio immediately, e.g. on barge-in.

        The generation bump makes the callback discard every queued chunk and
        the partially consumed remainder, so the stream falls silent within
        one callback period — no abort/close/recreate of the native stream.
        """
        self._discard_backlog()
        logger.info("[SPK] Playback flushed (barge-in)")

    def _discard_backlog(self, *, deactivate: bool = False) -> None:
        """Invalidate writes crossing a flush/stop and release unsent PCM."""
        replacement: queue.Queue[tuple[int, bytes]] = queue.Queue()
        with self._lock:
            if deactivate:
                self._active = False
            self._generation += 1
            previous_queue, self._queue = self._queue, replacement
            self._pending = b""
            self._queued_bytes = 0
            self._prebuffering = True
            self._prebuffer_wait_bytes = 0
        # Release the old backlog outside the callback's critical section.
        del previous_queue
        with self._level_lock:
            self._last_level = 0.0
            if deactivate:
                self._last_audible_at = None

    @property
    def is_playing(self) -> bool:
        """Return whether audible audio was output within the hangover window.

        True while non-silent PCM is being handed to PortAudio and for
        ``ECHO_HANGOVER_MS`` afterwards (room reverb tail). Prebuffer and
        underrun silence never count. Thread-safe; used as the mic echo gate.
        """
        with self._lock:
            if not self._active:
                return False
        with self._level_lock:
            last = self._last_audible_at
        if last is None:
            return False
        return (time.monotonic() - last) * 1000 <= ECHO_HANGOVER_MS

    @property
    def level(self) -> float:
        """Return the most recent output level, normalised to ``[0.0, 1.0]``."""
        with self._level_lock:
            return self._last_level

    @property
    def diagnostics(self) -> dict[str, float]:
        """Return a coherent snapshot with counters cumulative per instance.

        ``played_ms`` counts PCM handed to PortAudio, excluding silence; it
        is not a hardware playback clock. ``underruns`` counts callbacks
        padded with silence, including ordinary idle callbacks.
        """
        milliseconds_per_byte = 1000 / (OUTPUT_SAMPLE_RATE * _BYTES_PER_FRAME)
        with self._lock:
            return {
                "queued_ms": self._queued_bytes * milliseconds_per_byte,
                "high_water_ms": self._high_water_bytes * milliseconds_per_byte,
                "played_ms": self._played_bytes * milliseconds_per_byte,
                "underruns": float(self._underruns),
                "callback_status_count": float(self._callback_status_count),
            }

    # -- PortAudio callback (real-time thread; no blocking I/O) --

    def _audio_callback(self, outdata: Any, frames: int, _time: Any, status: Any) -> None:
        """Fill ``outdata`` with queued PCM, or silence when nothing is queued."""
        need = frames * _BYTES_PER_FRAME
        pos = 0
        with self._lock:
            if status:
                self._callback_status_count += 1
            if self._prebuffering:
                if self._queued_bytes >= _PREBUFFER_BYTES:
                    self._prebuffering = False
                elif self._queued_bytes > 0:
                    # Never strand a short tail: play it once it has waited
                    # a further prebuffer period without more audio arriving.
                    self._prebuffer_wait_bytes += need
                    if self._prebuffer_wait_bytes >= _PREBUFFER_BYTES:
                        self._prebuffering = False
                else:
                    self._prebuffer_wait_bytes = 0
            while pos < need and not self._prebuffering:
                if not self._pending:
                    try:
                        generation, chunk = self._queue.get_nowait()
                    except queue.Empty:
                        break  # underrun: the rest of the buffer stays silent
                    if generation != self._generation:
                        continue
                    if not self._active:
                        self._queued_bytes = max(0, self._queued_bytes - len(chunk))
                        continue
                    self._pending = chunk
                take = min(need - pos, len(self._pending))
                outdata[pos : pos + take] = self._pending[:take]
                self._pending = self._pending[take:]
                self._queued_bytes = max(0, self._queued_bytes - take)
                self._played_bytes += take
                pos += take
            if pos < need:
                self._underruns += 1
                if not self._prebuffering:
                    # Ran dry mid-stream: rebuffer before resuming.
                    self._prebuffering = True
                    self._prebuffer_wait_bytes = 0

        if pos < need:
            outdata[pos:need] = b"\x00" * (need - pos)
        # Level from the bytes actually handed to PortAudio, outside the lock.
        if pos:
            self._update_level(bytes(outdata[:pos]))
        else:
            with self._level_lock:
                self._last_level = 0.0

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
        level = min(rms / 32768.0, 1.0)
        with self._level_lock:
            self._last_level = level
            if level >= _AUDIBLE_LEVEL:
                self._last_audible_at = time.monotonic()
