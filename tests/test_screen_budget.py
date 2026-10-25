"""JPEG frame byte budget behavior."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

from PIL import Image

from media import screen


def noisy_image() -> Image.Image:
    return Image.effect_noise((512, 512), 100).convert("RGB")


def test_encode_steps_quality_down_to_fit_budget(monkeypatch: Any) -> None:
    monkeypatch.setattr(screen, "_cursor_position", lambda: None)
    image = noisy_image()
    baseline = screen._encode(image.copy(), {}, 512, 82)

    frame = screen._encode(image, {}, 512, 82, max_bytes=len(baseline) - 1)

    assert frame.quality < 82
    assert len(frame) <= len(baseline)
    assert frame.pixel_digest is not None


def test_encode_stops_at_minimum_even_when_over_budget(monkeypatch: Any) -> None:
    monkeypatch.setattr(screen, "_cursor_position", lambda: None)
    image = noisy_image()

    frame = screen._encode(image, {}, 512, 82, max_bytes=1)

    assert frame.quality == screen.MIN_QUALITY
    assert len(frame) > 1


def test_no_budget_encodes_once_at_requested_quality(monkeypatch: Any) -> None:
    monkeypatch.setattr(screen, "_cursor_position", lambda: None)
    saved_qualities: list[int] = []
    original_save = Image.Image.save

    def save(image: Image.Image, fp: Any, *args: Any, **kwargs: Any) -> None:
        saved_qualities.append(kwargs["quality"])
        original_save(image, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", save)
    frame = screen._encode(noisy_image(), {}, 512, 67, max_bytes=None)

    assert saved_qualities == [67]
    assert frame.quality == 67


def test_unchanged_frame_keeps_requested_quality(monkeypatch: Any) -> None:
    monkeypatch.setattr(screen, "_cursor_position", lambda: None)
    image = noisy_image()
    original = screen._encode(image, {}, 512, 82)

    unchanged = screen._encode(
        image.copy(), {}, 512, 67, prev_digest=original.pixel_digest, max_bytes=1
    )

    assert unchanged.unchanged
    assert unchanged == b""
    assert unchanged.quality == 67


def test_capture_jpeg_threads_max_bytes(monkeypatch: Any) -> None:
    calls: list[tuple[Any, ...]] = []

    class Grabber:
        def grab(self, *args: Any) -> screen.JpegFrame:
            calls.append(args)
            return screen._make_frame(b"jpeg", None, quality=55)

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(screen, "_grabber", Grabber())

    result = screen.capture_jpeg(2, 800, 76, max_bytes=1234)

    assert result == b"jpeg"
    assert calls == [(2, 800, 76, None, 1234)]


def test_screen_capture_stores_and_passes_max_bytes(monkeypatch: Any) -> None:
    passed: list[tuple[Any, ...]] = []

    class FakeMSS:
        monitors = [
            {"left": 0, "top": 0, "width": 4, "height": 4},
            {"left": 0, "top": 0, "width": 2, "height": 2},
        ]

        def __enter__(self) -> FakeMSS:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def grab(self, _region: Any) -> Any:
            return SimpleNamespace(size=(2, 2), bgra=bytes(16))

    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(MSS=FakeMSS))

    def encode(*args: Any, **kwargs: Any) -> screen.JpegFrame:
        passed.append(args + (kwargs["max_bytes"],))
        return screen._make_frame(b"jpeg", None)

    capture = screen.ScreenCapture(max_width=800, quality=76, max_bytes=4321)
    monkeypatch.setattr(screen, "_encode", encode)
    capture._running = True

    def publish(_frame: bytes) -> None:
        capture._running = False

    monkeypatch.setattr(capture, "_publish", publish)
    capture._capture()

    assert capture.max_bytes == 4321
    assert passed[0][3:] == (76, 4321)
