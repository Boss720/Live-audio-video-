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
import atexit
import contextlib
import hashlib
import io
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from obs.logging_setup import get_logger

logger = get_logger(__name__)


def _mss_factory(mss: Any) -> Any:
    """Return the screenshotter class. mss 10 renamed ``mss.mss`` to ``mss.MSS``."""
    return getattr(mss, "MSS", None) or mss.mss


DEFAULT_FPS = 1.0
# Small text (taskbar labels, code, menus) smears at 1024px/q70 with 4:2:0
# chroma, so frames default to 1280px / q82 / 4:4:4. Computer control needs to
# click precisely on small targets, so it gets 1568px (the model's native cap).
DEFAULT_MAX_WIDTH = 1280  # downscale wider screens to this before encoding
COMPUTER_USE_MAX_WIDTH = 1568
DEFAULT_QUALITY = 82  # JPEG quality
JPEG_SUBSAMPLING = 0  # 4:4:4 - no chroma smear on thin coloured text
_THUMB_SIZE = (64, 36)
_THUMB_SHIFT = 3  # keep the top 5 bits of gray so tiny noise does not flip the hash


class JpegFrame(bytes):
    """JPEG bytes carrying the digest of the raw pixels they were encoded from.

    ``unchanged`` is True (and the payload empty) when the capture matched the
    caller's ``prev_digest`` and encoding was skipped.
    """

    pixel_digest: bytes | None = None
    unchanged: bool = False


def _make_frame(data: bytes, digest: bytes | None, unchanged: bool = False) -> JpegFrame:
    frame = JpegFrame(data)
    frame.pixel_digest = digest
    frame.unchanged = unchanged
    return frame


def _pixel_digest(image: Any) -> bytes:
    """Hash a quantized 64x36 grayscale thumbnail (noise-tolerant, cheap)."""
    from PIL import Image

    thumb = image.convert("L").resize(_THUMB_SIZE, Image.BOX)  # type: ignore[attr-defined]
    quantized = bytes(v >> _THUMB_SHIFT for v in thumb.tobytes())
    return hashlib.blake2b(quantized, digest_size=16).digest()


def _cursor_position() -> tuple[int, int] | None:
    """Virtual-screen cursor position via Win32; None elsewhere or on failure."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)
    except Exception:
        return None


_CURSOR_SHAPE = ((0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12))


def _draw_cursor(image: Any, region: dict[str, Any], scale: float) -> None:
    """Draw a dark-outlined white arrow at the cursor (mss omits the cursor)."""
    pos = _cursor_position()
    if pos is None:
        return
    x = pos[0] - int(region.get("left", 0))
    y = pos[1] - int(region.get("top", 0))
    if not (0 <= x < image.width and 0 <= y < image.height):
        return
    from PIL import ImageDraw

    s = max(1.0, scale)
    points = [(x + px * s, y + py * s) for px, py in _CURSOR_SHAPE]
    draw = ImageDraw.Draw(image)
    draw.polygon(points, fill=(255, 255, 255))
    draw.line(points + [points[0]], fill=(0, 0, 0), width=max(1, round(1.5 * s)))


def _encode(
    image: Any,
    region: dict[str, Any],
    max_width: int,
    quality: int,
    prev_digest: bytes | None = None,
) -> JpegFrame:
    """Digest the raw pixels, then (unless unchanged) mark cursor, scale, encode."""
    from PIL import Image

    digest = _pixel_digest(image)
    # Fold the cursor (16 px buckets) into the digest: pointing at something on
    # a static screen must still send a frame showing where the cursor is.
    cursor = _cursor_position()
    if cursor is not None:
        bucket = f"{cursor[0] >> 4},{cursor[1] >> 4}".encode()
        digest = hashlib.blake2b(digest + bucket, digest_size=16).digest()
    if prev_digest is not None and digest == prev_digest:
        return _make_frame(b"", digest, unchanged=True)
    scale = image.width / max_width if image.width > max_width else 1.0
    _draw_cursor(image, region, scale)
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize(
            (max_width, height), Image.LANCZOS  # type: ignore[attr-defined]
        )
    buffer = io.BytesIO()
    image.save(
        buffer, format="JPEG", quality=quality, subsampling=JPEG_SUBSAMPLING
    )
    return _make_frame(buffer.getvalue(), digest)


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


class _OnDemandGrabber:
    """One dedicated thread that owns a long-lived mss instance.

    mss instances are thread-bound and building one costs tens of ms on
    Windows, so on-demand captures all run on this single worker.
    """

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen-grab")
        self._sct: Any = None

    def _grab(
        self,
        monitor: int,
        max_width: int,
        quality: int,
        prev_digest: bytes | None,
    ) -> JpegFrame:
        from PIL import Image

        for attempt in (0, 1):
            try:
                if self._sct is None:
                    import mss

                    self._sct = _mss_factory(mss)()
                region = _resolve_monitor(self._sct, monitor)
                shot = self._sct.grab(region)
                break
            except Exception:
                self._close_sct()
                if attempt:
                    raise
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return _encode(image, region, max_width, quality, prev_digest)

    def _close_sct(self) -> None:
        sct, self._sct = self._sct, None
        if sct is not None:
            with contextlib.suppress(Exception):
                sct.close()

    def grab(
        self,
        monitor: int,
        max_width: int,
        quality: int,
        prev_digest: bytes | None,
    ) -> JpegFrame:
        return self._pool.submit(
            self._grab, monitor, max_width, quality, prev_digest
        ).result()

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self._pool.submit(self._close_sct).result(timeout=2)
        self._pool.shutdown(wait=False)


_grabber: _OnDemandGrabber | None = None
_grabber_lock = threading.Lock()


def shutdown_capture() -> None:
    """Close the shared on-demand mss instance (idempotent)."""
    global _grabber
    with _grabber_lock:
        grabber, _grabber = _grabber, None
    if grabber is not None:
        grabber.shutdown()


atexit.register(shutdown_capture)


def capture_jpeg(
    monitor: int = 1,
    max_width: int | None = None,
    quality: int = DEFAULT_QUALITY,
    *,
    prev_digest: bytes | None = None,
    computer_use: bool = False,
) -> JpegFrame:
    """Grab a single frame as JPEG bytes, outside the streaming thread.

    One-shot counterpart of ``ScreenCapture`` - used to push a fresh screenshot
    to the model right after a computer-use action without waiting for the
    next tick of the ambient 1 fps stream. Reuses one mss instance on a
    dedicated thread. The result's ``pixel_digest`` identifies the raw pixels;
    when it equals ``prev_digest`` encoding is skipped and ``unchanged`` is set.
    """
    global _grabber
    if max_width is None:
        max_width = COMPUTER_USE_MAX_WIDTH if computer_use else DEFAULT_MAX_WIDTH
    with _grabber_lock:
        if _grabber is None:
            _grabber = _OnDemandGrabber()
        grabber = _grabber
    return grabber.grab(monitor, max_width, quality, prev_digest)


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
        max_width: int | None = None,
        quality: int = DEFAULT_QUALITY,
        computer_use: bool = False,
    ) -> None:
        self.fps = fps
        self.monitor = monitor
        if max_width is None:
            max_width = COMPUTER_USE_MAX_WIDTH if computer_use else DEFAULT_MAX_WIDTH
        self.max_width = max_width
        self.quality = quality
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop_event = threading.Event()
        self._lifecycle_lock = asyncio.Lock()

    # -- Public API --

    async def start(self) -> None:
        """Begin capturing the screen."""
        async with self._lifecycle_lock:
            if self._running:
                return
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Screen capture thread is still stopping")
            # Validation and capture each create MSS on their own thread.
            region = await asyncio.to_thread(validate_monitor, self.monitor)
            self._discard_pending_frame()
            self._stop_event.clear()
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
        async with self._lifecycle_lock:
            self.monitor = monitor
            # Validation can finish after stop. Preserve the shutdown sentinel
            # so a waiting sender still learns that capture has ended.
            if self._running:
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
        async with self._lifecycle_lock:
            was_running = self._running
            self._running = False
            self._stop_event.set()
            if self._loop is not None and not self._loop.is_closed():
                if self._loop is asyncio.get_running_loop():
                    self._enqueue_sentinel()
                else:
                    self._loop.call_soon_threadsafe(self._enqueue_sentinel)
            if self._thread is not None:
                await asyncio.to_thread(self._thread.join, timeout=2)
                if self._thread.is_alive():
                    logger.warning("Screen capture thread is still stopping")
                else:
                    self._thread = None
            if was_running:
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

                    frame = _encode(image, region, self.max_width, self.quality)
                    if self.monitor == selected_monitor:
                        self._publish(frame)
                except Exception as exc:
                    logger.warning("Screen capture error: %s", exc)

                # Pace to the target frame rate, accounting for capture time
                elapsed = time.monotonic() - started
                if self._running and elapsed < interval:
                    self._stop_event.wait(interval - elapsed)

    def _publish(self, frame: bytes) -> None:
        """Hand a frame to the event loop, replacing any frame not yet sent."""
        if self._loop is None or not self._running:
            return

        def put() -> None:
            if not self._running:
                return
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
