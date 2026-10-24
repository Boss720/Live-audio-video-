"""StatusStrip widget behaviour."""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from app.status_strip import StatusStrip
from core.events import ConnectionState, DiagnosticsUpdate, UsageUpdate
from obs.metrics import TimingSummary


@pytest.fixture
def strip(qtbot: Any) -> StatusStrip:
    w = StatusStrip()
    qtbot.addWidget(w)
    w.show()
    return w


def _lbl(w: StatusStrip, name: str) -> QLabel:
    child = w.findChild(QLabel, name)
    assert child is not None
    return child


def _btn(w: StatusStrip, name: str) -> QPushButton:
    child = w.findChild(QPushButton, name)
    assert child is not None
    return child


def test_connection_states(strip: StatusStrip) -> None:
    chip = _lbl(strip, "statusStateChip")
    for state in ConnectionState:
        strip.set_connection(state, "d")
        assert chip.text()
    strip.set_connection(ConnectionState.LIVE)
    assert chip.text() == "Live"
    assert "#34D399" in chip.styleSheet()
    strip.set_connection(ConnectionState.ERROR, "boom")
    assert "#FB7185" in chip.styleSheet()
    assert chip.toolTip() == "boom"


def test_model_tier_privacy(strip: StatusStrip) -> None:
    strip.set_model("Gemini 3.8 Live")
    strip.set_tier("Tier 1")
    strip.set_privacy("strict")
    assert _lbl(strip, "statusModel").text() == "Gemini 3.8 Live · Tier 1 · strict"


def test_usage(strip: StatusStrip) -> None:
    cost = _lbl(strip, "statusCost")
    strip.update_usage(UsageUpdate(total_tokens=10, estimated_usd=0.042))
    assert cost.text() == "$0.0420 session"
    assert "10" in cost.toolTip()
    strip.update_usage(UsageUpdate(estimated_usd=None))
    assert cost.text() == "Free"


def test_latency(strip: StatusStrip) -> None:
    lat = _btn(strip, "statusLatency")
    strip.update_diagnostics(DiagnosticsUpdate())
    assert lat.text() == "- ms"
    ts = TimingSummary(samples=0, p50_ms=0, p95_ms=0, p99_ms=0, latest_ms=0, max_ms=0)
    strip.update_diagnostics(DiagnosticsUpdate(timings={"speech_end_to_first_audio_ms": ts}))
    assert lat.text() == "- ms"
    ts = TimingSummary(samples=5, p50_ms=612, p95_ms=900, p99_ms=950, latest_ms=600, max_ms=990)
    strip.update_diagnostics(DiagnosticsUpdate(timings={"speech_end_to_first_audio_ms": ts}))
    assert lat.text() == "612 ms p50"
    assert "900" in lat.toolTip()
    assert "5 samples" in lat.toolTip()


def test_yolo_and_screen(strip: StatusStrip) -> None:
    yolo = _lbl(strip, "statusYolo")
    screen = _lbl(strip, "statusScreen")
    assert not yolo.isVisible()
    strip.set_yolo(True)
    assert yolo.isVisible()
    assert yolo.text() == "YOLO on"
    strip.set_yolo(False)
    assert not yolo.isVisible()
    assert not screen.isVisible()
    strip.set_screen(True, 2)
    assert screen.isVisible()
    assert screen.text().endswith("Screen 2")
    strip.set_screen(False, 2)
    assert not screen.isVisible()


def test_signals_and_disconnect_enabled(strip: StatusStrip, qtbot: Any) -> None:
    disc = _btn(strip, "statusDisconnect")
    assert not disc.isEnabled()
    strip.set_connected_controls(True)
    assert disc.isEnabled()
    with qtbot.waitSignal(strip.disconnect_requested, timeout=1000):
        qtbot.mouseClick(disc, Qt.MouseButton.LeftButton)
    with qtbot.waitSignal(strip.settings_requested, timeout=1000):
        qtbot.mouseClick(_btn(strip, "statusSettings"), Qt.MouseButton.LeftButton)
    with qtbot.waitSignal(strip.diagnostics_requested, timeout=1000):
        qtbot.mouseClick(_btn(strip, "statusLatency"), Qt.MouseButton.LeftButton)
    strip.set_connected_controls(False)
    assert not disc.isEnabled()
