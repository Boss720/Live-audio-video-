from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from app.diagnostics_page import DiagnosticsPage, build_report, format_report
from core.events import DiagnosticsUpdate, UsageUpdate
from obs.metrics import TimingSummary


def _diag() -> DiagnosticsUpdate:
    return DiagnosticsUpdate(
        timings={
            "speech_end_to_first_audio_ms": TimingSummary(10, 612.0, 940.0, 990.0, 600.0, 1000.0)
        },
        audio={"mic.dropped_chunks": 0.0, "speaker.underruns": 1.0, "mic.queued_ms": 80.0},
        counters={
            "frames_sent": 41,
            "frame_bytes_sent": 4100,
            "frame_duplicates_dropped": 233,
            "frame_wait_timeouts": 2,
        },
    )


def test_build_report_full() -> None:
    rep = build_report(
        _diag(),
        usage=UsageUpdate(prompt_tokens=12000),
        reconnects=3,
        last_disconnect=("1011", "boom"),
    )
    s = rep["summary"]
    assert s["speech_end_p50_ms"] == 612.0
    assert s["speech_end_p95_ms"] == 940.0
    assert s["mic_dropped"] == 0.0
    assert s["speaker_underruns"] == 1.0
    assert s["frames_sent"] == 41
    assert s["frames_skipped"] == 233
    assert s["reconnects"] == 3
    assert s["context_prompt_tokens"] == 12000
    assert rep["timings"]["speech_end_to_first_audio_ms"]["samples"] == 10


def test_build_report_empty() -> None:
    rep = build_report(None)
    assert rep["timings"] == {}
    s = rep["summary"]
    assert s["speech_end_p50_ms"] is None
    assert s["mic_dropped"] is None
    assert s["bytes_saved_estimate"] is None
    assert s["context_prompt_tokens"] is None
    assert format_report(rep)
    assert build_report(DiagnosticsUpdate())["summary"]["frames_sent"] is None


def test_bytes_saved_math() -> None:
    assert build_report(_diag())["summary"]["bytes_saved_estimate"] == pytest.approx(233 * 100)
    zero = DiagnosticsUpdate(counters={"frames_sent": 0, "frame_duplicates_dropped": 5})
    assert build_report(zero)["summary"]["bytes_saved_estimate"] == 0


def test_format_report_has_p50_line() -> None:
    text = format_report(build_report(_diag()))
    assert "612 ms p50" in text
    assert "p95 940 ms" in text
    assert "Local application measurements" in text


def test_json_serialisable() -> None:
    json.dumps(build_report(_diag(), usage=UsageUpdate(prompt_tokens=1)))
    json.dumps(build_report(None))


def test_page_updates_tiles(qtbot: Any) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)
    assert page.objectName() == "diagnosticsPage"
    page.update_diagnostics(_diag())
    page.update_usage(UsageUpdate(prompt_tokens=12000))
    page.note_reconnect("1011", "boom")
    tiles = page._tiles
    assert tiles["speech"].value.text() == "612 ms p50"
    assert tiles["frames"].value.text() == "41 / 233"
    assert tiles["reconnects"].value.text() == "1"
    assert "1011 boom" in tiles["reconnects"].sub.text()
    assert tiles["context"].value.text() == "12.0k"
    assert page._table.rowCount() == 1 + 3 + 4


def test_copy_report(qtbot: Any) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)
    page.update_diagnostics(_diag())
    page._copy_btn.click()
    assert "612 ms p50" in QApplication.clipboard().text()


def test_export_json(qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)
    page.update_diagnostics(_diag())
    out = tmp_path / "d.json"
    monkeypatch.setattr(page, "_ask_export_path", lambda: str(out))
    page._export_btn.click()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["summary"]["frames_sent"] == 41
