"""Non-modal Diagnostics page: tiles, full measurement table, copy/export."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.theme import (
    CYAN_300,
    GLASS_BORDER,
    GLASS_FILL,
    NAVY_900,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from core.events import DiagnosticsUpdate, UsageUpdate

DASH = "–"
FOOTER_NOTE = "Local application measurements; not provider model or hardware latency."
_SPEECH_KEY = "speech_end_to_first_audio_ms"


def build_report(
    diag: DiagnosticsUpdate | None,
    *,
    usage: UsageUpdate | None = None,
    reconnects: int = 0,
    last_disconnect: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Return a JSON-serialisable report; missing data becomes None."""
    timings: dict[str, dict[str, float | int]] = {}
    audio: dict[str, float] = {}
    counters: dict[str, int] = {}
    if diag is not None:
        timings = {name: dict(asdict(t)) for name, t in diag.timings.items()}
        audio = dict(diag.audio)
        counters = dict(diag.counters)

    speech = timings.get(_SPEECH_KEY)
    sent = counters.get("frames_sent")
    dupes = counters.get("frame_duplicates_dropped")
    bytes_sent = counters.get("frame_bytes_sent")
    saved: float | None
    if sent and dupes is not None and bytes_sent is not None:
        saved = dupes * (bytes_sent / sent)
    elif diag is not None:
        saved = 0.0
    else:
        saved = None

    summary: dict[str, Any] = {
        "speech_end_p50_ms": speech["p50_ms"] if speech else None,
        "speech_end_p95_ms": speech["p95_ms"] if speech else None,
        "mic_dropped": audio.get("mic.dropped_chunks"),
        "mic_queued_ms": audio.get("mic.queued_ms"),
        "mic_oldest_chunk_ms": audio.get("mic.oldest_chunk_ms"),
        "mic_echo_suppressed": audio.get("mic.echo_suppressed_chunks"),
        "speaker_underruns": audio.get("speaker.underruns"),
        "speaker_high_water_ms": audio.get("speaker.high_water_ms"),
        "frames_sent": sent,
        "frames_skipped": dupes,
        "bytes_saved_estimate": saved,
        "frame_wait_timeouts": counters.get("frame_wait_timeouts"),
        "reconnects": reconnects,
        "last_disconnect_code": last_disconnect[0] if last_disconnect else None,
        "last_disconnect_detail": last_disconnect[1] if last_disconnect else None,
        "context_prompt_tokens": usage.prompt_tokens if usage is not None else None,
    }
    return {"timings": timings, "audio": audio, "counters": counters, "summary": summary}


def _num(value: Any) -> str:
    return DASH if value is None else format(value, "g")


def _ms(value: Any) -> str:
    return DASH if value is None else f"{value:.0f} ms"


def _mb(value: Any) -> str:
    return DASH if value is None else f"{value / 1_000_000:.1f} MB"


def _context(value: Any) -> str:
    return DASH if value is None else f"{value / 1000:.1f}k"


def _last(summary: dict[str, Any]) -> str:
    code = summary.get("last_disconnect_code")
    if code is None:
        return DASH
    detail = summary.get("last_disconnect_detail") or ""
    return f"{code} {detail}".strip()


def format_report(report: dict[str, Any]) -> str:
    """Human-readable text for the clipboard."""
    s = report.get("summary", {})
    lines = [
        "Diagnostics",
        f"Speech end -> first audio: {_ms(s.get('speech_end_p50_ms'))} p50 "
        f"| p95 {_ms(s.get('speech_end_p95_ms'))}",
        f"Mic chunks dropped: {_num(s.get('mic_dropped'))} "
        f"| queue {_ms(s.get('mic_queued_ms'))} | oldest {_ms(s.get('mic_oldest_chunk_ms'))} "
        f"| echo-suppressed {_num(s.get('mic_echo_suppressed'))}",
        f"Speaker underruns: {_num(s.get('speaker_underruns'))} "
        f"| high water {_ms(s.get('speaker_high_water_ms'))}",
        f"Frames sent / skipped: {_num(s.get('frames_sent'))} / {_num(s.get('frames_skipped'))} "
        f"| ~{_mb(s.get('bytes_saved_estimate'))} saved",
        f"Frame wait timeouts: {_num(s.get('frame_wait_timeouts'))}",
        f"Reconnects: {_num(s.get('reconnects'))} | last: {_last(s)}",
        f"Context: {_context(s.get('context_prompt_tokens'))}",
        "",
        "Timings (latest / p50 / p95 / p99 ms, samples)",
    ]
    for name, t in report.get("timings", {}).items():
        lines.append(
            f"  {name}: {t['latest_ms']:.1f} / {t['p50_ms']:.1f} / {t['p95_ms']:.1f} / "
            f"{t['p99_ms']:.1f} ({t['samples']})"
        )
    lines.append("Audio")
    lines.extend(f"  {k}: {v:g}" for k, v in report.get("audio", {}).items())
    lines.append("Counters")
    lines.extend(f"  {k}: {v}" for k, v in report.get("counters", {}).items())
    lines.extend(["", FOOTER_NOTE])
    return "\n".join(lines)


class _Tile(QWidget):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setStyleSheet(
            f"background: {GLASS_FILL}; border: 1px solid {GLASS_BORDER}; border-radius: 10px;"
        )
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet(f"color: {TEXT_MUTED}; border: none; background: transparent;")
        self.value = QLabel(DASH)
        self.value.setStyleSheet(
            f"color: {CYAN_300}; font-size: 20px; font-weight: 600; "
            "border: none; background: transparent;"
        )
        self.sub = QLabel("")
        self.sub.setWordWrap(True)
        self.sub.setStyleSheet(f"color: {TEXT_MUTED}; border: none; background: transparent;")
        for w in (heading, self.value, self.sub):
            layout.addWidget(w)

    def set(self, value: str, sub: str = "") -> None:
        self.value.setText(value)
        self.sub.setText(sub)


class DiagnosticsPage(QDialog):
    """Non-modal diagnostics view fed by the main window's event handler."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("diagnosticsPage")
        self.setWindowTitle("Diagnostics")
        self.setModal(False)
        self.resize(760, 640)
        self.setStyleSheet(f"QDialog#diagnosticsPage {{ background: {NAVY_900}; }}")
        self._diag: DiagnosticsUpdate | None = None
        self._usage: UsageUpdate | None = None
        self._reconnects = 0
        self._last_disconnect: tuple[str, str] | None = None

        root = QVBoxLayout(self)
        head = QHBoxLayout()
        title = QLabel("Diagnostics")
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 18px; font-weight: 600;")
        head.addWidget(title)
        head.addStretch(1)
        self._copy_btn = QPushButton("Copy report")
        self._copy_btn.setObjectName("copyReportButton")
        self._export_btn = QPushButton("Export JSON")
        self._export_btn.setObjectName("exportJsonButton")
        head.addWidget(self._copy_btn)
        head.addWidget(self._export_btn)
        root.addLayout(head)

        grid = QGridLayout()
        names = [
            ("speech", "Speech end → first audio"),
            ("mic", "Mic chunks dropped"),
            ("speaker", "Speaker underruns"),
            ("frames", "Frames sent / skipped"),
            ("timeouts", "Frame wait timeouts"),
            ("reconnects", "Reconnects"),
            ("context", "Context"),
        ]
        self._tiles: dict[str, _Tile] = {}
        for i, (key, label) in enumerate(names):
            tile = _Tile(label)
            self._tiles[key] = tile
            grid.addWidget(tile, i // 3, i % 3)
        root.addLayout(grid)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(["Name", "Latest", "p50", "p95", "p99", "Samples"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self._table, 1)

        note = QLabel(FOOTER_NOTE)
        note.setStyleSheet(f"color: {TEXT_MUTED};")
        root.addWidget(note)

        self._copy_btn.clicked.connect(self._copy_report)
        self._export_btn.clicked.connect(self._export_json)
        self._refresh()

    # -- public API --

    def update_diagnostics(self, event: DiagnosticsUpdate) -> None:
        self._diag = event
        self._refresh()

    def update_usage(self, event: UsageUpdate) -> None:
        self._usage = event
        self._refresh()

    def note_reconnect(self, code: str, detail: str) -> None:
        self._reconnects += 1
        self._last_disconnect = (code, detail)
        self._refresh()

    def report(self) -> dict[str, Any]:
        return build_report(
            self._diag,
            usage=self._usage,
            reconnects=self._reconnects,
            last_disconnect=self._last_disconnect,
        )

    # -- internals --

    def _ask_export_path(self) -> str:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export diagnostics", "diagnostics.json", "JSON (*.json)"
        )
        return path

    def _copy_report(self) -> None:
        QApplication.clipboard().setText(format_report(self.report()))

    def _export_json(self) -> None:
        path = self._ask_export_path()
        if path:
            Path(path).write_text(json.dumps(self.report(), indent=2), encoding="utf-8")

    def _refresh(self) -> None:
        rep = self.report()
        s = rep["summary"]
        t = self._tiles
        t["speech"].set(_ms(s["speech_end_p50_ms"]) + " p50", f"p95 {_ms(s['speech_end_p95_ms'])}")
        t["mic"].set(
            _num(s["mic_dropped"]),
            f"queue {_ms(s['mic_queued_ms'])} · oldest {_ms(s['mic_oldest_chunk_ms'])}"
            f" · echo-suppressed {_num(s['mic_echo_suppressed'])}",
        )
        t["speaker"].set(
            _num(s["speaker_underruns"]), f"high water {_ms(s['speaker_high_water_ms'])}"
        )
        t["frames"].set(
            f"{_num(s['frames_sent'])} / {_num(s['frames_skipped'])}",
            f"pixel-hash dedup · ~{_mb(s['bytes_saved_estimate'])} saved",
        )
        t["timeouts"].set(_num(s["frame_wait_timeouts"]))
        t["reconnects"].set(_num(s["reconnects"]), f"last: {_last(s)}")
        t["context"].set(_context(s["context_prompt_tokens"]))

        rows: list[list[str]] = []
        for name, tm in rep["timings"].items():
            rows.append(
                [
                    name,
                    f"{tm['latest_ms']:.1f}",
                    f"{tm['p50_ms']:.1f}",
                    f"{tm['p95_ms']:.1f}",
                    f"{tm['p99_ms']:.1f}",
                    str(tm["samples"]),
                ]
            )
        for k, v in rep["audio"].items():
            rows.append([k, f"{v:g}", DASH, DASH, DASH, DASH])
        for k, c in rep["counters"].items():
            rows.append([k, str(c), DASH, DASH, DASH, DASH])
        self._table.setRowCount(len(rows))
        for r, cells in enumerate(rows):
            for c, text in enumerate(cells):
                self._table.setItem(r, c, QTableWidgetItem(text))
