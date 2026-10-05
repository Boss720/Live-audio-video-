"""Screen capture for the Gemini Live Agent — streams the desktop as video.

The Live API takes realtime video as individual JPEG frames. Screen content
changes slowly compared to speech, so this captures at a low frame rate (1 fps
by default) and downscales before encoding — full-resolution frames burn
bandwidth and context for no gain in what the model can read.

Capture runs on its own thread: ``mss`` is blocking and its instances are bound
to the thread that created them, so it cannot share the default executor.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import threading
import time
from typing import Any

from obs.logging_setup import get_logger

logger = get_logger(__name__)


def _mss_factory(mss: Any) -> Any:
    """Return the screenshotter class. mss 10 renamed ``mss.mss`` to ``mss.MSS``."""
    return getattr(mss, "MSS", None) or mss.mss


DEFAULT_FPS = 1.0
# 1024/q70 is not the accuracy bottleneck it looks like. Measured against the
# live model on a real 1920x1080 desktop, 1024px/q70 and native 1920px/q85
# frames scored identically (6/6) on screen-reading questions ranging from
# taskbar icons to sidebar entries to a small model badge — while the native
# frame cost 3.5x the bytes (250KB vs 72KB). Raise these only with an eval
# showing an actual gain; see scripts/vision_eval.py.
DEFAULT_MAX_WIDTH = 1024  # downscale wider screens to this before encoding
DEFAULT_QUALITY = 70  # JPEG quality


def _resolve_monitor(sct: Any, monitor: int) -> dict[str, Any]:
    """Return the mss region for ``monitor``, falling back to a valid index."""
    monitors = sct.monitors
    if 0 <= monitor < len(monitors):
        return dict(monitors[monitor])
    return dict(monitors[1] if len(monitors) > 1 else monitors[0])


def validate_monitor(monitor: int) -> dict[str, Any]:
    """Validate a monitor index and return its MSS region."""
    import mss

    with _mss_factory(mss)() as sct:
        count = len(sct.monitors) - 1  # index 0 is the virtual "all monitors"
        if monitor < 0 or monitor > count:
            raise ValueError(
                f"Monitor {monitor} does not exist "
                f"(available: 0 for all, 1-{count} for individual screens)"
            )
        return dict(sct.monitors[monitor])


def monitor_geometry(monitor: int = 1) -> dict[str, int]:
    """Return ``{"left", "top", "width", "height"}`` for a monitor in pixels.

    Used to map normalized computer-use coordinates onto the physical desktop.
    """
    import mss  # imported here so --help works without the dependency

    with _mss_factory(mss)() as sct:
        region = _resolve_monitor(sct, monitor)
    return {
        "left": int(region["left"]),
        "top": int(region["top"]),
        "width": int(region["width"]),
        "height": int(region["height"]),
    }


def capture_jpeg(
    monitor: int = 1,
    max_width: int = DEFAULT_MAX_WIDTH,
    quality: int = DEFAULT_QUALITY,
) -> bytes:
    """Grab a single frame as JPEG bytes, outside the streaming thread.

    One-shot counterpart of ``ScreenCapture`` — used to push a fresh screenshot
    to the model right after a computer-use action without waiting for the
    next tick of the ambient 1 fps stream.
    """
    import mss
    from PIL import Image

    with _mss_factory(mss)() as sct:
        region = _resolve_monitor(sct, monitor)
        shot = sct.grab(region)

    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize(
            (max_width, height), Image.LANCZOS  # type: ignore[attr-defined]
        )

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


class ScreenCapture:
    """Captures the desktop as JPEG frames at a fixed frame rate.

    Only the most recent frame is kept. If the sender falls behind, older
    frames are dropped rather than queued — stale screenshots are worse than
    no screenshot.
    """

    def __init__(
        self,
        fps: float = DEFAULT_FPS,
        monitor: int = 1,
        max_width: int = DEFAULT_MAX_WIDTH,
        quality: int = DEFAULT_QUALITY,
    ) -> None:
        self.fps = fps
        self.monitor = monitor
        self.max_width = max_width
        self.quality = quality
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    # -- Public API --

    async def start(self) -> None:
        """Begin capturing the screen."""
        # Validate the monitor index up front rather than failing per-frame.
        region = validate_monitor(self.monitor)

        self._loop = asyncio.get_running_loop()
        self._running = True
        self._thread = threading.Thread(target=self._capture, daemon=True)
        self._thread.start()
        logger.info(
            "[SCREEN] Sharing monitor %d (%dx%d) at %g fps",
            self.monitor,
            region["width"],
            region["height"],
            self.fps,
        )

    async def set_monitor(self, monitor: int) -> None:
        """Switch the active capture and discard any queued frame from the old monitor."""
        region = await asyncio.to_thread(validate_monitor, monitor)
        self.monitor = monitor
        self._discard_pending_frame()
        logger.info(
            "[SCREEN] Switched to monitor %d (%dx%d)",
            monitor,
            region["width"],
            region["height"],
        )

    async def read_frame(self) -> bytes:
        """Return the next JPEG frame. Blocks until one is available."""
        frame = await self._queue.get()
        if frame is None:
            raise asyncio.CancelledError("Screen capture stopped")
        return frame

    async def stop(self) -> None:
        """Stop capturing."""
        if self._running:
            self._running = False
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._enqueue_sentinel)
            if self._thread is not None:
                self._thread.join(timeout=2)
                self._thread = None
            logger.info("[SCREEN] Screen sharing stopped")

    # -- Capture thread --

    def _capture(self) -> None:
        import mss
        from PIL import Image

        interval = 1.0 / self.fps if self.fps > 0 else 1.0

        with _mss_factory(mss)() as sct:
            selected_monitor = self.monitor
            region = sct.monitors[selected_monitor]
            while self._running:
                if self.monitor != selected_monitor:
                    selected_monitor = self.monitor
                    region = sct.monitors[selected_monitor]
                started = time.monotonic()
                try:
                    shot = sct.grab(region)
                    image = Image.frombytes(
                        "RGB", shot.size, shot.bgra, "raw", "BGRX"
                    )

                    if image.width > self.max_width:
                        height = round(image.height * self.max_width / image.width)
                        image = image.resize(
                            (self.max_width, height), Image.LANCZOS  # type: ignore[attr-defined]
                        )

                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=self.quality)
                    if self.monitor == selected_monitor:
                        self._publish(buffer.getvalue())
                except Exception as exc:
                    logger.warning("Screen capture error: %s", exc)

                # Pace to the target frame rate, accounting for capture time
                elapsed = time.monotonic() - started
                if self._running and elapsed < interval:
                    time.sleep(interval - elapsed)

    def _publish(self, frame: bytes) -> None:
        """Hand a frame to the event loop, replacing any frame not yet sent."""
        if self._loop is None:
            return

        def put() -> None:
            if self._queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()  # drop the stale frame
            self._queue.put_nowait(frame)

        try:
            self._loop.call_soon_threadsafe(put)
        except RuntimeError:
            logger.debug("Failed to publish screen frame: event loop closed")

    def _enqueue_sentinel(self) -> None:
        """Put a sentinel to unblock read_frame during shutdown."""
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        self._queue.put_nowait(None)

    def _discard_pending_frame(self) -> None:
        """Drop a frame captured from the previous monitor selection."""
        with contextlib.suppress(asyncio.QueueEmpty):
            self._queue.get_nowait()
