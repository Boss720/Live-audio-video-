"""Minimal PySide6 main window for the Gemini Live Agent.

Consumes the same frozen event/command vocabulary as the headless driver. The
asyncio Supervisor lives on a ``Bridge`` QThread; the main window only touches
Qt objects and ``AgentEvent`` / ``AgentCommand`` dataclasses.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from enum import StrEnum
from typing import Any

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from app.approvals_rail import ApprovalsRail
from app.bridge import Bridge
from app.chat_view import ChatView
from app.diagnostics_page import DiagnosticsPage
from app.settings_dialog import LIVE_FIELDS, MODEL_OPTIONS, SettingsDialog
from app.status_strip import StatusStrip
from app.theme import DANGER, MAIN_QSS, SUCCESS, TEXT_MUTED
from core.commands import (
    ApproveTool,
    CancelTool,
    DenyTool,
    GatingMode,
    SendText,
    SetGatingMode,
    SetMicGate,
    SetShareScreen,
)
from core.events import (
    AgentEvent,
    AudioLevel,
    ConnectionState,
    ConnectionStateChanged,
    ContextReset,
    DiagnosticsUpdate,
    DisconnectReason,
    PartialTranscript,
    RateLimitHit,
    SessionError,
    SessionExpiring,
    TextNotSent,
    ToolApprovalRequested,
    ToolCallCancelled,
    ToolCallReceived,
    ToolResultSent,
    TranscriptSource,
    TurnComplete,
    UsageUpdate,
)
from obs.logging_setup import get_run_id, setup_logging
from settings.secrets import get_api_key, get_free_api_key, set_api_key
from settings.settings import AppSettings, canonicalize_model_name

_METER_INTERVAL_MS = 33
_RAIL_WIDTH = 340
_TOOL_SUMMARY_CHARS = 120
_TOOL_ARG_CHARS = 60
_TIER_NAMES = {"free": "Free", "tier1": "Tier 1"}

logger = logging.getLogger(__name__)


class _MicState(StrEnum):
    """GUI-side microphone state machine (voice UX)."""

    OFF = "off"
    LIVE = "live"
    MUTED = "muted"


_MIC_CHIP_STYLES = {
    _MicState.OFF: f"color: {TEXT_MUTED};",
    _MicState.LIVE: f"color: {SUCCESS}; font-weight: bold;",
    _MicState.MUTED: f"color: {DANGER}; font-weight: bold;",
}

_MIC_CHIP_TEXT = {
    _MicState.OFF: "Mic: off",
    _MicState.LIVE: "Mic: live",
    _MicState.MUTED: "Mic: muted",
}


class MainWindow(QMainWindow):
    """Primary GUI window for the Gemini Live Agent."""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._bridge: Bridge | None = None
        self._last_source: TranscriptSource | None = None
        self._tool_rows: set[str] = set()
        self._tier = ""
        self._connected_mode: str | None = None
        self._mic_muted = False
        self._mic_state = _MicState.OFF
        self._sharing_active = False
        self._force_quit = False
        self._tray: QSystemTrayIcon | None = None
        self._tray_message_shown = False
        self._settings_dialog: SettingsDialog | None = None
        self._diagnostics_page = DiagnosticsPage(self)

        self.setWindowTitle("Gemini Live Agent")
        self.setMinimumSize(960, 600)
        self._build_ui()
        self._tier = self._tier_label()
        self._refresh_strip()
        self._setup_tray()

    # -- UI construction --

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("centralRoot")
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(6)

        # Status strip: connection, model, latency, cost, YOLO, screen, actions.
        self._strip = StatusStrip()
        self._strip.settings_requested.connect(self._open_settings)
        self._strip.disconnect_requested.connect(self._on_disconnect_requested)
        self._strip.diagnostics_requested.connect(self._show_diagnostics)
        main_layout.addWidget(self._strip)

        # Raw connection state text; the mic/meter logic and tests key off it.
        self._status_label = QLabel("Disconnected")
        self._status_label.setObjectName("statusChip")

        # Screen-share indicator banner (visible only while sharing + connected)
        self._share_banner = QLabel("● Sharing screen — the model can see your screen")
        self._share_banner.setObjectName("shareBanner")
        self._share_banner.setAccessibleName("Screen sharing indicator")
        self._share_banner.setVisible(False)
        main_layout.addWidget(self._share_banner)

        # Centre: chat (left, stretch) + right rail (approvals, meters, activity)
        centre = QHBoxLayout()
        centre.setSpacing(8)

        chat_column = QVBoxLayout()
        transcript_actions = QHBoxLayout()
        self._copy_transcript_btn = QPushButton("Copy transcript")
        self._copy_transcript_btn.clicked.connect(self._copy_transcript)
        self._clear_transcript_btn = QPushButton("Clear transcript")
        self._clear_transcript_btn.clicked.connect(self._clear_transcript)
        transcript_actions.addStretch()
        transcript_actions.addWidget(self._copy_transcript_btn)
        transcript_actions.addWidget(self._clear_transcript_btn)
        chat_column.addLayout(transcript_actions)
        self._chat = ChatView()
        self._chat.setAccessibleName("Conversation transcript")
        self._chat.setMinimumHeight(70)
        chat_column.addWidget(self._chat, 1)
        centre.addLayout(chat_column, 1)

        rail_column = QVBoxLayout()
        rail_column.setContentsMargins(0, 0, 0, 0)
        self._approvals = ApprovalsRail()
        self._approvals.approve_requested.connect(self._approve_tool)
        self._approvals.deny_requested.connect(self._deny_tool)
        self._approvals.cancel_requested.connect(self._cancel_tool)
        self._approvals_scroll = QScrollArea()
        self._approvals_scroll.setAccessibleName("Tool approvals")
        self._approvals_scroll.setWidgetResizable(True)
        self._approvals_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._approvals_scroll.setWidget(self._approvals)
        self._approvals_scroll.setMinimumHeight(160)
        rail_column.addWidget(self._approvals_scroll, 2)

        meter_layout = QHBoxLayout()
        self._mic_meter = QProgressBar()
        self._mic_meter.setRange(0, 100)
        self._mic_meter.setTextVisible(True)
        self._mic_meter.setFormat("Mic: %p%")
        self._speaker_meter = QProgressBar()
        self._speaker_meter.setRange(0, 100)
        self._speaker_meter.setTextVisible(True)
        self._speaker_meter.setFormat("Speaker: %p%")
        meter_layout.addWidget(self._mic_meter)
        meter_layout.addWidget(self._speaker_meter)
        rail_column.addLayout(meter_layout)

        self._activity = QListWidget()
        self._activity.setAccessibleName("Activity feed")
        rail_column.addWidget(self._activity, 1)

        rail_widget = QWidget()
        rail_widget.setFixedWidth(_RAIL_WIDTH)
        rail_widget.setLayout(rail_column)
        centre.addWidget(rail_widget)
        main_layout.addLayout(centre, 1)

        # Composer: input, Send, Mute mic, mic chip, Connect
        bottom_layout = QHBoxLayout()
        self._input_edit = QLineEdit()
        self._input_edit.setPlaceholderText("Type a message and press Enter...")
        self._input_edit.setAccessibleName("Message input")
        self._input_edit.returnPressed.connect(self._on_send_clicked)
        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send_clicked)
        self._mute_btn = QPushButton("Mute mic")
        self._mute_btn.setCheckable(True)
        self._mute_btn.setAccessibleName("Toggle microphone mute")
        self._mute_btn.toggled.connect(self._on_mute_toggled)
        self._mic_chip = QLabel(_MIC_CHIP_TEXT[_MicState.OFF])
        self._mic_chip.setStyleSheet(_MIC_CHIP_STYLES[_MicState.OFF])
        self._mic_chip.setAccessibleName("Microphone state")
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setObjectName("accentButton")
        # Never keyboard-focusable. A focused QPushButton fires on Space, and
        # when computer use types into this window instead of the app it was
        # aiming at — which happens whenever the target fails to come to the
        # front — the first space in the text ends the session. Losing a
        # session to a keystroke that was meant for a search box is not a
        # trade worth the tab-order convenience; the button is one click away
        # and computer use now refuses to type into its own window at all.
        self._connect_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        bottom_layout.addWidget(self._input_edit)
        bottom_layout.addWidget(self._send_btn)
        bottom_layout.addWidget(self._mute_btn)
        bottom_layout.addWidget(self._mic_chip)
        bottom_layout.addWidget(self._connect_btn)
        main_layout.addLayout(bottom_layout)

        # Usage panel in the status bar (updated on UsageUpdate events)
        self._usage_label = QLabel("0 tokens")
        self._usage_label.setAccessibleName("Token usage")
        self.statusBar().addPermanentWidget(self._usage_label)
        self._diagnostics_label = QLabel("Local diagnostics: no samples")
        self._diagnostics_label.setAccessibleName("Local runtime diagnostics")
        self._diagnostics_label.setToolTip(
            "Local application spans only; not provider model or hardware latency."
        )
        self.statusBar().addWidget(self._status_label)
        self.statusBar().addWidget(self._diagnostics_label, 1)

        # Meter timer
        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(_METER_INTERVAL_MS)
        self._meter_timer.timeout.connect(self._update_meters)

        # Menu bar shortcut for exit
        exit_action = QAction("E&xit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        self.menuBar().addAction(exit_action)

        diagnostics_action = QAction("&Diagnostics", self)
        diagnostics_action.setShortcut("Ctrl+Shift+D")
        diagnostics_action.triggered.connect(self._show_diagnostics)
        self.menuBar().addAction(diagnostics_action)

        # Ctrl+M toggles the mic gate from anywhere in the window.
        mute_shortcut = QShortcut(QKeySequence("Ctrl+M"), self)
        mute_shortcut.activated.connect(self._mute_btn.toggle)

        # Ctrl+, opens the settings dialog.
        self._settings_shortcut = QShortcut(QKeySequence("Ctrl+,"), self)
        self._settings_shortcut.activated.connect(self._open_settings)

    # -- Settings handling --

    def _tier_label(self) -> str:
        """Best-effort tier the session starts on, from the routing settings."""
        if self._settings.privacy_mode == "strict" or not self._settings.prefer_free_tier:
            return "Tier 1"
        return "Free"

    def _refresh_strip(self) -> None:
        model = canonicalize_model_name(self._settings.model)
        labels = {value: label for label, value in MODEL_OPTIONS}
        self._strip.set_model(labels.get(model, model))
        self._strip.set_tier(self._tier)
        self._strip.set_privacy(self._settings.privacy_mode)
        self._strip.set_yolo(self._settings.yolo)
        self._strip.set_screen(self._sharing_active, self._settings.screen_monitor)

    @Slot()
    def _open_settings(self) -> SettingsDialog:
        """Open the (modal) settings dialog; returns it so callers can inspect it."""
        dialog = SettingsDialog(self._settings, self)
        dialog.settings_saved.connect(self._on_settings_saved)
        self._settings_dialog = dialog
        dialog.open()
        return dialog

    @Slot(object)
    def _on_settings_saved(self, new_settings: AppSettings) -> None:
        old_values = self._settings.model_dump()
        try:
            new_settings.save()
        except Exception as exc:
            QMessageBox.warning(self, "Settings error", str(exc))
            return
        changed = {k for k, v in new_settings.model_dump().items() if old_values.get(k) != v}
        self._settings = new_settings
        if self._bridge is not None:
            self._apply_live_settings(changed)
            # workspace_root follows working_dir; headless is forced off by the dialog.
            reconnect = len(changed - LIVE_FIELDS - {"workspace_root", "headless"})
            if reconnect:
                self.statusBar().showMessage(f"{reconnect} changes apply on reconnect", 6000)
        if self._bridge is None:
            self._tier = self._tier_label()
        self._refresh_strip()
        self._refresh_mic_state()

    def _apply_live_settings(self, changed: set[str]) -> None:
        """Push the settings that have a live command into the running session."""
        if self._bridge is None:
            return
        settings = self._settings
        if "yolo" in changed:
            mode = GatingMode.YOLO if settings.yolo else GatingMode.GUARDED
            self._bridge.send_command(SetGatingMode(mode=mode))
        if changed & {"share_screen", "screen_monitor"}:
            # Computer control needs the screen feed; never disable it here.
            self._sharing_active = settings.share_screen or settings.computer_use_enabled
            self._bridge.send_command(
                SetShareScreen(enabled=self._sharing_active, monitor=settings.screen_monitor)
            )
            self._update_share_banner()
        # minimize_to_tray is GUI-local; closeEvent reads it from self._settings.

    @Slot()
    def _show_diagnostics(self) -> None:
        self._diagnostics_page.show()
        self._diagnostics_page.raise_()
        self._diagnostics_page.activateWindow()

    @Slot()
    def _copy_transcript(self) -> None:
        text = self._chat.plain_text()
        if text:
            QApplication.clipboard().setText(text)
            self._add_activity("Transcript copied to clipboard")

    @Slot()
    def _clear_transcript(self) -> None:
        self._chat.clear()
        self._tool_rows.clear()
        self._last_source = None

    # -- System tray --

    def _setup_tray(self) -> None:
        """Create the tray icon when the platform supports it."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.info("System tray not available; minimize-to-tray disabled")
            return

        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Gemini Live Agent")

        menu = QMenu()
        show_action = menu.addAction("Show / Hide")
        show_action.triggered.connect(self._on_tray_show_hide)
        mute_action = menu.addAction("Mute mic")
        mute_action.triggered.connect(self._mute_btn.toggle)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._on_tray_quit)
        self._tray.setContextMenu(menu)

        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_show_hide(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()

    def _on_tray_quit(self) -> None:
        self._force_quit = True
        self.close()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._on_tray_show_hide()

    # -- Activity feed / usage / mic chip / share banner --

    def _add_activity(self, text: str) -> None:
        """Append a timestamped line to the activity feed."""
        stamp = datetime.now().strftime("%H:%M:%S")
        self._activity.addItem(f"{stamp}  {text}")
        self._activity.scrollToBottom()

    def _update_usage(self, event: UsageUpdate) -> None:
        parts = [f"{event.total_tokens} tokens"]
        detail = f"in {event.prompt_tokens} / out {event.completion_tokens}"
        if event.cached_tokens:
            detail += f" / cached {event.cached_tokens}"
        parts.append(detail)
        if event.estimated_usd:
            parts.append(f"~${event.estimated_usd:.4f}")
        else:
            parts.append("Free")
        self._usage_label.setText("  ·  ".join(parts))
        self._usage_label.setToolTip(
            f"audio {event.audio_tokens} · video {event.video_tokens}"
        )

    def _set_mic_state(self, state: _MicState) -> None:
        if state == self._mic_state:
            return
        self._mic_state = state
        self._mic_chip.setText(_MIC_CHIP_TEXT[state])
        self._mic_chip.setStyleSheet(_MIC_CHIP_STYLES[state])
        self._add_activity(_MIC_CHIP_TEXT[state])

    def _session_mode(self) -> str:
        """Mode the live session connected with (settings edits apply on reconnect)."""
        return (self._connected_mode or self._settings.mode).lower()

    def _refresh_mic_state(self) -> None:
        """Recompute the mic chip from connection state, mode, and mute flag."""
        connected = self._bridge is not None and self._status_label.text() in (
            ConnectionState.LIVE.value,
            ConnectionState.DRAINING.value,
        )
        wants_mic = self._session_mode() != "text"
        if not connected or not wants_mic:
            self._set_mic_state(_MicState.OFF)
        elif self._mic_muted:
            self._set_mic_state(_MicState.MUTED)
        else:
            self._set_mic_state(_MicState.LIVE)
        self._refresh_meter_timer()

    def _refresh_meter_timer(self) -> None:
        active = (
            self.isVisible() and not self.isMinimized() and self._bridge is not None
            and self._session_mode() != "text"
            and self._status_label.text() in (
                ConnectionState.LIVE.value, ConnectionState.DRAINING.value
            )
        )
        if active:
            self._meter_timer.start()
        else:
            self._meter_timer.stop()
            self._mic_meter.setValue(0)
            self._speaker_meter.setValue(0)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._refresh_meter_timer()

    def hideEvent(self, event: Any) -> None:
        self._meter_timer.stop()
        super().hideEvent(event)

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        if hasattr(self, "_meter_timer"):
            self._refresh_meter_timer()

    def _update_share_banner(self) -> None:
        self._share_banner.setVisible(self._sharing_active)
        self._strip.set_screen(self._sharing_active, self._settings.screen_monitor)

    # -- Bridge lifecycle --

    @Slot()
    def _on_connect_clicked(self) -> None:
        if self._bridge is not None:
            self._bridge.stop()
            return

        settings = self._settings
        try:
            settings.save()
        except Exception as exc:
            QMessageBox.warning(self, "Settings error", str(exc))
            return

        self._bridge = Bridge(settings, run_id=get_run_id())
        self._bridge.event_emitted.connect(self._on_event)
        self._bridge.finished_with_reason.connect(self._on_bridge_finished)
        self._bridge.start()
        self._diagnostics_label.setText("Local diagnostics: no samples")

        self._connect_btn.setText("Disconnect")
        self._strip.set_connected_controls(True)
        self._connected_mode = settings.mode
        self._tier = self._tier_label()
        self._refresh_strip()

        self._sharing_active = settings.share_screen or settings.computer_use_enabled
        self._update_share_banner()
        self._add_activity(f"Connecting (model {settings.model})...")

        # Reflect UI toggles in the freshly started core.
        if settings.yolo:
            self._bridge.send_command(SetGatingMode(mode=GatingMode.YOLO))
        self._bridge.send_command(SetMicGate(open=not self._mic_muted))
        self._bridge.send_command(
            SetShareScreen(
                # Computer control needs the screen feed; never disable it here.
                enabled=settings.share_screen or settings.computer_use_enabled,
                monitor=settings.screen_monitor,
            )
        )

    @Slot()
    def _on_disconnect_requested(self) -> None:
        if self._bridge is not None:
            self._on_connect_clicked()

    @Slot(str)
    def _on_bridge_finished(self, reason: str) -> None:
        """Clean up when the Supervisor loop exits."""
        if self._bridge is not None:
            self._bridge.wait()
            self._bridge.deleteLater()
            self._bridge = None
        self._status_label.setText(f"Disconnected ({reason})")
        self._connect_btn.setText("Connect")
        self._strip.set_connection(ConnectionState.DISCONNECTED, reason)
        self._strip.set_connected_controls(False)
        self._chat.set_speaking(False)
        self._connected_mode = None
        self._mic_meter.setValue(0)
        self._speaker_meter.setValue(0)
        self._sharing_active = False
        self._update_share_banner()
        self._refresh_mic_state()
        self._add_activity(f"Disconnected ({reason})")
        self._approvals.resolve_all("Session disconnected", session_finished=True)

    # -- Event handling --

    @Slot(object)  # PySide's stub loses AgentEvent unions.
    def _on_event(self, event: AgentEvent) -> None:
        if isinstance(event, ConnectionStateChanged):
            self._status_label.setText(event.state.value)
            self._strip.set_connection(event.state, event.detail)
            self._add_activity(
                f"Connection: {event.state.value}"
                + (f" — {event.detail}" if event.detail else "")
            )
            if event.detail:
                self._status_label.setToolTip(event.detail)
            if event.state == ConnectionState.DISCONNECTED:
                self._connect_btn.setText("Connect")
                self._chat.set_speaking(False)
            elif event.state == ConnectionState.LIVE:
                self._connect_btn.setText("Disconnect")
            self._strip.set_connected_controls(self._bridge is not None)
            self._refresh_mic_state()
            if event.state == ConnectionState.DISCONNECTED:
                self._approvals.resolve_all("Session disconnected")
        elif isinstance(event, PartialTranscript):
            self._chat.append_partial(event.text, event.source, event.turn_id)
            self._last_source = event.source
            if event.source == TranscriptSource.MODEL:
                self._chat.set_speaking(True)
        elif isinstance(event, TextNotSent):
            self._chat.add_system(f"[NOT SENT] {event.text}\nReason: {event.reason}")
            if not self._input_edit.text():
                self._input_edit.setText(event.text)
        elif isinstance(event, TurnComplete):
            self._chat.complete_turn(
                event.source, event.text, event.turn_id, interrupted=event.interrupted
            )
            if event.source == TranscriptSource.MODEL:
                self._chat.set_speaking(False)
            self._last_source = None
        elif isinstance(event, ToolCallReceived):
            self._add_tool_row(event.call_id, event.name, event.args)
            self._add_activity(f"Tool call: {event.name}")
        elif isinstance(event, ToolApprovalRequested):
            self._add_tool_row(event.call_id, event.name, event.args)
            self._approvals.add_request(event)
            self._add_activity(f"Approval requested: {event.name}")
        elif isinstance(event, ToolResultSent):
            self._approvals.set_result(event)
            summary = event.result.strip().splitlines()[0] if event.result.strip() else ""
            self._chat.update_tool_row(
                event.call_id, ok=event.ok, summary=summary[:_TOOL_SUMMARY_CHARS]
            )
            self._add_activity(
                f"Tool result: {event.name} -> {'OK' if event.ok else 'FAILED'}"
            )
        elif isinstance(event, ToolCallCancelled):
            self._approvals.mark_cancelled(event)
            for call_id in event.call_ids:
                self._chat.update_tool_row(call_id, ok=False, summary="cancelled")
            self._add_activity(f"Tool cancelled: {event.reason}")
        elif isinstance(event, ContextReset):
            self._chat.add_system(f"[RESET] {event.reason}")
            self._add_activity(f"Context reset: {event.reason}")
        elif isinstance(event, SessionExpiring):
            notice = (
                f"Session expiring in {event.seconds}s; reconnecting..."
                if event.seconds is not None
                else "Session expiring; reconnecting..."
            )
            self._chat.add_system(f"[EXPIRING] {notice}")
            self._add_activity(notice)
        elif isinstance(event, SessionError):
            self._add_activity(f"Error: {event.message}")
            if event.fatal:
                QMessageBox.critical(self, "Session error", event.message)
            else:
                self._chat.add_system(f"[ERROR] {event.message}")
        elif isinstance(event, UsageUpdate):
            self._update_usage(event)
            self._strip.update_usage(event)
            self._diagnostics_page.update_usage(event)
        elif isinstance(event, DiagnosticsUpdate):
            self._update_diagnostics(event)
            self._strip.update_diagnostics(event)
            self._diagnostics_page.update_diagnostics(event)
        elif isinstance(event, RateLimitHit):
            self._tier = _TIER_NAMES.get(event.tier.lower(), event.tier)
            self._strip.set_tier(self._tier)
            self._add_activity(f"Rate limit: {event.model} ({self._tier})")
        elif isinstance(event, AudioLevel):
            # Levels are polled via _update_meters; ignore pushed events.
            pass
        else:
            # Not part of the AgentEvent union today; the session may forward it.
            self._note_disconnect(event)

    def _note_disconnect(self, event: object) -> None:
        if isinstance(event, DisconnectReason) and event.code != "user_request":
            self._diagnostics_page.note_reconnect(event.code, event.detail)

    def _add_tool_row(self, call_id: str, name: str, args: dict[str, Any]) -> None:
        # ToolCallReceived and ToolApprovalRequested both announce a call.
        if call_id in self._tool_rows:
            return
        self._tool_rows.add(call_id)
        target = args.get("path") or args.get("command")
        if target is None and args:
            target = next(iter(args.values()))
        detail = " ".join(str(target).split())[:_TOOL_ARG_CHARS] if target is not None else ""
        self._chat.add_tool_row(call_id, name, detail)

    def _update_diagnostics(self, event: DiagnosticsUpdate) -> None:
        details = ["Local application measurements; not provider model or hardware latency."]
        for name, timing in event.timings.items():
            details.append(
                f"{name}: latest {timing.latest_ms:.1f} ms; p50 {timing.p50_ms:.1f}, "
                f"p95 {timing.p95_ms:.1f}, p99 {timing.p99_ms:.1f} ms ({timing.samples} samples)"
            )
        details.extend(f"{name}: {value:g}" for name, value in event.audio.items())
        details.extend(f"{name}: {value}" for name, value in event.counters.items())
        measurements = len(event.timings) + len(event.audio) + len(event.counters)
        self._diagnostics_label.setText(
            f"Local diagnostics: {measurements} measurements · hover for details"
        )
        self._diagnostics_label.setToolTip("\n".join(details))

    # -- Tool approvals --

    @Slot(str)
    def _approve_tool(self, call_id: str) -> None:
        if self._bridge is not None:
            self._bridge.send_command(ApproveTool(call_id=call_id))
            self._approvals.mark_approved(call_id)

    @Slot(str)
    def _deny_tool(self, call_id: str) -> None:
        if self._bridge is not None:
            self._bridge.send_command(DenyTool(call_id=call_id))

    @Slot(str)
    def _cancel_tool(self, call_id: str) -> None:
        if self._bridge is not None:
            self._bridge.send_command(CancelTool(call_ids=[call_id]))

    # -- Input and meters --

    @Slot()
    def _on_send_clicked(self) -> None:
        text = self._input_edit.text().strip()
        if not text or self._bridge is None:
            return
        self._bridge.send_command(SendText(text=text))
        self._chat.add_user_text(text)
        self._input_edit.clear()

    @Slot(bool)
    def _on_mute_toggled(self, checked: bool) -> None:
        self._mic_muted = checked
        self._mute_btn.setText("Unmute mic" if checked else "Mute mic")
        if self._bridge is not None:
            self._bridge.send_command(SetMicGate(open=not checked))
        self._refresh_mic_state()

    @Slot()
    def _update_meters(self) -> None:
        if self._bridge is None:
            self._mic_meter.setValue(0)
            self._speaker_meter.setValue(0)
            return
        self._mic_meter.setValue(int(self._bridge.mic_level * 100))
        self._speaker_meter.setValue(int(self._bridge.speaker_level * 100))

    # -- Window close --

    def closeEvent(self, event: Any) -> None:
        self._meter_timer.stop()
        # Minimize to the tray instead of quitting when the setting is on.
        if (
            not self._force_quit
            and self._settings.minimize_to_tray
            and self._tray is not None
            and self._tray.isVisible()
        ):
            self.hide()
            if not self._tray_message_shown:
                self._tray_message_shown = True
                self._tray.showMessage(
                    "Gemini Live Agent",
                    "Still running in the tray. Use Quit in the tray menu to exit.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
            event.ignore()
            return

        if self._bridge is not None:
            self._bridge.stop()
            self._bridge.wait()
            self._bridge.deleteLater()
            self._bridge = None
        if self._tray is not None:
            self._tray.hide()
        event.accept()


def run_gui(settings: AppSettings) -> int:
    """Create the QApplication, prompt for an API key if needed, and run the GUI."""
    app = QApplication(sys.argv)
    app.setStyleSheet(MAIN_QSS)

    has_tier1 = get_api_key() is not None or settings.api_key_tier1 is not None
    has_free = get_free_api_key() is not None or settings.api_key_free is not None
    needs_key = not has_tier1 and (settings.privacy_mode == "strict" or not has_free)

    if needs_key:
        prompt = (
            "Strict privacy requires a Tier 1 API key. Enter your Tier 1 API key:"
            if settings.privacy_mode == "strict"
            else "No Google API key found. Enter your API key:"
        )
        key, ok = QInputDialog.getText(
            None,  # The startup dialog intentionally has no parent.
            "API key required",
            prompt,
            QLineEdit.EchoMode.Password,
        )
        if not ok or not key:
            QMessageBox.critical(None, "Error", "An API key is required to continue.")
            return 1
        set_api_key(key)

    setup_logging(run_id=get_run_id(), debug=settings.debug)

    window = MainWindow(settings)
    window.show()
    return app.exec()
