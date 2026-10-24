"""Wave 3 frame quality: sharper JPEGs, cursor overlay, mss reuse, pixel dedup."""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

import media.screen as screen


class FakeSct:
    instances = 0

    def __init__(self, state: dict[str, Any]) -> None:
        FakeSct.instances += 1
        self.state = state
        pixels = state["image"]
        self.closed = False
        self.monitors = [
            {"left": 0, "top": 0, "width": pixels.width, "height": pixels.height},
            {"left": 100, "top": 50, "width": pixels.width, "height": pixels.height},
        ]

    def grab(self, region: Any) -> SimpleNamespace:
        rgb = self.state["image"].convert("RGB")
        bgra = Image.merge("RGBA", (*rgb.split()[::-1], Image.new("L", rgb.size, 255)))
        return SimpleNamespace(size=rgb.size, bgra=bgra.tobytes())

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_mss(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"image": Image.new("RGB", (1920, 1080), (30, 60, 90))}
    FakeSct.instances = 0
    monkeypatch.setattr(
        screen, "_mss_factory", lambda _m: lambda: FakeSct(state)
    )
    monkeypatch.setattr(screen, "_cursor_position", lambda: None)
    screen.shutdown_capture()
    yield state
    screen.shutdown_capture()


def test_default_and_computer_use_widths(fake_mss: dict[str, Any]) -> None:
    normal = Image.open(io.BytesIO(screen.capture_jpeg(1)))
    assert normal.width == 1280
    cu = Image.open(io.BytesIO(screen.capture_jpeg(1, computer_use=True)))
    assert cu.width == 1568
    assert screen.ScreenCapture().max_width == 1280
    assert screen.ScreenCapture(computer_use=True).max_width == 1568


def test_quality_and_subsampling_applied(
    fake_mss: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real_save = Image.Image.save

    def spy(self: Image.Image, fp: Any, format: Any = None, **params: Any) -> None:
        seen.update(params)
        real_save(self, fp, format=format, **params)

    monkeypatch.setattr(Image.Image, "save", spy)
    screen.capture_jpeg(1)
    assert seen["quality"] == 82
    assert seen["subsampling"] == 0


def test_cursor_drawn_at_monitor_relative_position(
    fake_mss: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_mss["image"] = Image.new("RGB", (1280, 720), (0, 0, 0))
    # monitor 1 origin is (100, 50) -> frame position (300, 200)
    monkeypatch.setattr(screen, "_cursor_position", lambda: (400, 250))
    out = Image.open(io.BytesIO(screen.capture_jpeg(1))).convert("L")
    assert out.getpixel((303, 206)) > 200  # white arrow body
    assert out.getpixel((700, 500)) < 20  # untouched background


def test_cursor_outside_frame_or_unavailable_is_noop(
    fake_mss: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    img = Image.new("RGB", (100, 100), (0, 0, 0))
    monkeypatch.setattr(screen, "_cursor_position", lambda: (5000, 5000))
    screen._draw_cursor(img, {"left": 0, "top": 0}, 1.0)
    assert img.getextrema() == ((0, 0), (0, 0), (0, 0))
    monkeypatch.setattr(screen, "_cursor_position", lambda: None)
    screen._draw_cursor(img, {"left": 0, "top": 0}, 1.0)
    assert img.getextrema() == ((0, 0), (0, 0), (0, 0))


def test_mss_instance_reused_across_captures(fake_mss: dict[str, Any]) -> None:
    for _ in range(3):
        screen.capture_jpeg(1)
    assert FakeSct.instances == 1
    screen.shutdown_capture()
    screen.capture_jpeg(1)
    assert FakeSct.instances == 2


def test_unchanged_pixels_skip_encode(
    fake_mss: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = screen.capture_jpeg(1)
    assert first and first.pixel_digest and not first.unchanged

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("encode must be skipped")

    monkeypatch.setattr(Image.Image, "save", boom)
    second = screen.capture_jpeg(1, prev_digest=first.pixel_digest)
    assert second.unchanged and second == b""
    assert second.pixel_digest == first.pixel_digest


def test_small_noise_still_dedups_and_real_change_does_not(
    fake_mss: dict[str, Any],
) -> None:
    base = Image.new("RGB", (1280, 720), (100, 100, 100))
    fake_mss["image"] = base
    first = screen.capture_jpeg(1)
    noisy = base.copy()
    noisy.putpixel((10, 10), (101, 99, 100))
    noisy.putpixel((500, 300), (100, 100, 101))
    fake_mss["image"] = noisy
    assert screen.capture_jpeg(1, prev_digest=first.pixel_digest).unchanged

    changed = base.copy()
    for x in range(200, 600):
        for y in range(200, 400):
            changed.putpixel((x, y), (250, 250, 250))
    fake_mss["image"] = changed
    third = screen.capture_jpeg(1, prev_digest=first.pixel_digest)
    assert not third.unchanged and third
    assert third.pixel_digest != first.pixel_digest
