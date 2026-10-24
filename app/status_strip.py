"""Top status strip: connection, model, latency, cost, YOLO, screen, actions."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from app.theme import (
    CYAN_300,
    DANGER,
    GLASS_BORDER,
    GLASS_FILL,
    SUCCESS,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from core.events import ConnectionState, DiagnosticsUpdate, UsageUpdate

_STATE_STYLE: dict[ConnectionState, tuple[str, str]] = {
    ConnectionState.IDLE: ("Idle", TEXT_MUTED),
    ConnectionState.CONNECTING: ("Connecting", CYAN_300),
    ConnectionState.LIVE: ("Live", SUCCESS),
    ConnectionState.DRAINING: ("Draining", CYAN_300),
    ConnectionState.RECONNECTING: ("Reconnecting", CYAN_300),
    ConnectionState.DISCONNECTING: ("Disconnecting", CYAN_300),
    ConnectionState.DISCONNECTED: ("Disconnected", TEXT_MUTED),
    ConnectionState.ERROR: ("Error", DANGER),
    ConnectionState.FAILED: ("Failed", DANGER),
}

_LATENCY_KEY = "speech_end_to_first_audio_ms"


class StatusStrip(QFrame):
    settings_requested = Signal()
    disconnect_requested = Signal()
    diagnostics_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusStrip")
        self.setStyleSheet(
            f"QFrame#statusStrip {{ background: {GLASS_FILL};"
            f" border: 1px solid {GLASS_BORDER}; border-radius: 10px; }}"
            f" QFrame#statusStrip QLabel {{ color: {TEXT_PRIMARY}; background: transparent; }}"
        )
        self._model = ""
        self._tier = ""
        self._privacy = ""

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(10)

        self._state_chip = self._label("statusStateChip", "Idle")
        self._model_label = self._label("statusModel", "")
        self._latency = QPushButton("- ms")
        self._latency.setObjectName("statusLatency")
        self._latency.setFlat(True)
        self._latency.clicked.connect(self.diagnostics_requested)
        self._cost = self._label("statusCost", "Free")
        self._yolo = self._label("statusYolo", "YOLO on")
        self._yolo.setStyleSheet(f"color: {DANGER}; font-weight: 600;")
        self._yolo.hide()
        self._screen = self._label("statusScreen", "")
        self._screen.setStyleSheet(f"color: {SUCCESS};")
        self._screen.hide()
        self._settings_btn = QPushButton("Settings")
        self._settings_btn.setObjectName("statusSettings")
        self._settings_btn.clicked.connect(self.settings_requested)
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setObjectName("statusDisconnect")
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self.disconnect_requested)

        row.addWidget(self._state_chip)
        row.addWidget(self._model_label)
        row.addWidget(self._latency)
        row.addWidget(self._cost)
        row.addWidget(self._yolo)
        row.addWidget(self._screen)
        row.addStretch(1)
        row.addWidget(self._settings_btn)
        row.addWidget(self._disconnect_btn)

        self.set_connection(ConnectionState.IDLE)

    def _label(self, name: str, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName(name)
        return lbl

    def set_connection(self, state: ConnectionState, detail: str = "") -> None:
        text, colour = _STATE_STYLE.get(state, (str(state), TEXT_MUTED))
        self._state_chip.setText(text)
        self._state_chip.setStyleSheet(f"color: {colour}; font-weight: 600;")
        self._state_chip.setToolTip(detail)

    def _refresh_model(self) -> None:
        parts = [p for p in (self._model, self._tier, self._privacy) if p]
        self._model_label.setText(" · ".join(parts))

    def set_model(self, label: str) -> None:
        self._model = label
        self._refresh_model()

    def set_tier(self, tier: str) -> None:
        self._tier = tier
        self._refresh_model()

    def set_privacy(self, mode: str) -> None:
        self._privacy = mode
        self._refresh_model()

    def update_usage(self, event: UsageUpdate) -> None:
        if event.estimated_usd is None:
            self._cost.setText("Free")
        else:
            self._cost.setText(f"${event.estimated_usd:.4f} session")
        self._cost.setToolTip(
            f"Prompt {event.prompt_tokens} (cached {event.cached_tokens})\n"
            f"Completion {event.completion_tokens}\n"
            f"Audio {event.audio_tokens} / Video {event.video_tokens}\n"
            f"Total {event.total_tokens} tokens"
        )

    def update_diagnostics(self, event: DiagnosticsUpdate) -> None:
        timing = event.timings.get(_LATENCY_KEY)
        if timing is None or timing.samples == 0:
            self._latency.setText("- ms")
            self._latency.setToolTip("No latency samples yet")
            return
        self._latency.setText(f"{timing.p50_ms:.0f} ms p50")
        self._latency.setToolTip(f"p95 {timing.p95_ms:.0f} ms, {timing.samples} samples")

    def set_yolo(self, on: bool) -> None:
        self._yolo.setVisible(on)

    def set_screen(self, sharing: bool, monitor: int) -> None:
        self._screen.setText(f"● Screen {monitor}")
        self._screen.setVisible(sharing)

    def set_connected_controls(self, connected: bool) -> None:
        self._disconnect_btn.setEnabled(connected)
