"""Behavioral desktop checks with no provider, microphone, or screen capture."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from app.bridge import Bridge
from app.main_window import MainWindow
from app.settings_dialog import LIVE_FIELDS, SettingsDialog
from app.theme import MAIN_QSS
from core.commands import (
    ApproveTool,
    CancelTool,
    DenyTool,
    GatingMode,
    SetGatingMode,
    SetShareScreen,
)
from core.events import (
    ConnectionState,
    ConnectionStateChanged,
    DiagnosticsUpdate,
    DisconnectReason,
    PartialTranscript,
    RateLimitHit,
    TextNotSent,
    ToolApprovalRequested,
    ToolCallCancelled,
    ToolCallReceived,
    ToolResultSent,
    TranscriptSource,
    TurnComplete,
    UsageUpdate,
)
from obs.metrics import TimingSummary
from settings.settings import AppSettings


class _StubBridge:
    mic_level = 0.2
    speaker_level = 0.4

    def stop(self) -> None:
        pass

    def wait(self) -> None:
        pass

    def deleteLater(self) -> None:
        pass


@pytest.fixture
def window(qtbot: Any, monkeypatch: Any, tmp_path: Path) -> Iterator[MainWindow]:
    monkeypatch.setattr("media.devices.list_input_devices", lambda: [])
    monkeypatch.setattr("media.devices.list_output_devices", lambda: [])
    monkeypatch.setattr("app.main_window.QSystemTrayIcon.isSystemTrayAvailable", lambda: False)
    result = MainWindow(
        AppSettings(
            mode="text",
            working_dir=tmp_path,
            workspace_root=tmp_path,
            minimize_to_tray=False,
            wake_word_enabled=False,
        )
    )
    qtbot.addWidget(result)
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    result.setFont(QFont("Segoe UI", 10))
    result.setStyleSheet(MAIN_QSS)
    result.resize(960, 600)
    result.show()
    qtbot.waitExposed(result)
    yield result
    result._bridge = None


def _no_save(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "settings.settings.user_config_dir",
        lambda appname, appauthor=None, **kwargs: str(tmp_path / "cfg"),
    )


class _RecordingBridge(_StubBridge):
    def __init__(self) -> None:
        self.commands: list[Any] = []

    def send_command(self, command: Any) -> None:
        self.commands.append(command)


def test_compact_layout_keeps_conversation_and_composer_usable(window: MainWindow) -> None:
    assert window.height() == 600
    assert window._chat.height() >= 70
    assert window._input_edit.isVisible()
    assert window._input_edit.mapTo(window, window._input_edit.rect().bottomRight()).y() < 600
    assert window._connect_btn.focusPolicy() == Qt.FocusPolicy.NoFocus
    # The right rail sits beside the chat, not under it.
    rail_x = window._approvals_scroll.mapTo(window, window._approvals_scroll.rect().topLeft()).x()
    chat_right = window._chat.mapTo(window, window._chat.rect().topRight()).x()
    assert rail_x >= chat_right


def test_settings_save_roundtrip_through_dialog(
    window: MainWindow, monkeypatch: Any, tmp_path: Path
) -> None:
    _no_save(monkeypatch, tmp_path)
    dialog = window._open_settings()
    assert "0 changes apply on reconnect" in dialog._footer_label.text()
    dialog._bindings["audio_chunk_duration_ms"].set(20)
    dialog._bindings["screen_deduplicate"].set(True)
    assert "2 changes apply on reconnect" in dialog._footer_label.text()
    dialog._save_btn.click()
    assert window._settings.audio_chunk_duration_ms == 20
    assert window._settings.screen_deduplicate is True
    loaded = AppSettings.load()
    assert loaded.audio_chunk_duration_ms == 20
    assert loaded.screen_deduplicate is True


def test_ctrl_comma_opens_settings_dialog(window: MainWindow) -> None:
    assert window._settings_shortcut.key().toString() == "Ctrl+,"
    window.activateWindow()
    QTest.keyClick(window, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
    dialog = window._settings_dialog
    assert isinstance(dialog, SettingsDialog)
    assert dialog.isVisible()
    dialog.close()


def test_strip_settings_button_opens_dialog(window: MainWindow) -> None:
    window._strip.settings_requested.emit()
    assert window._settings_dialog is not None
    assert window._settings_dialog.isVisible()
    window._settings_dialog.close()


def test_live_fields_are_exactly_the_ones_with_live_commands() -> None:
    assert set(LIVE_FIELDS) == {"yolo", "share_screen", "screen_monitor", "minimize_to_tray"}


def test_saving_yolo_while_connected_sends_gating_mode(
    window: MainWindow, monkeypatch: Any, tmp_path: Path
) -> None:
    _no_save(monkeypatch, tmp_path)
    bridge = _RecordingBridge()
    window._bridge = cast(Bridge, bridge)
    dialog = window._open_settings()
    dialog._bindings["yolo"].set(True)
    dialog._save_btn.click()
    assert bridge.commands == [SetGatingMode(mode=GatingMode.YOLO)]
    assert window._settings.yolo is True
    assert not window._strip._yolo.isHidden()
    assert window.statusBar().currentMessage() == ""

    sharing = not window._settings.share_screen
    dialog = window._open_settings()
    dialog._bindings["yolo"].set(False)
    dialog._bindings["share_screen"].set(sharing)
    dialog._bindings["voice"].set("Kore")
    dialog._save_btn.click()
    assert bridge.commands[1] == SetGatingMode(mode=GatingMode.GUARDED)
    share = bridge.commands[2]
    assert isinstance(share, SetShareScreen)
    assert share.enabled is sharing
    assert window._sharing_active is sharing
    assert window.statusBar().currentMessage() == "1 changes apply on reconnect"
    window._bridge = None


@pytest.mark.parametrize("size", [(960, 600), (1200, 800)])
def test_single_approval_actions_fit_initial_viewport(
    window: MainWindow, qtbot: Any, size: tuple[int, int]
) -> None:
    window.resize(*size)
    window._on_event(
        ToolApprovalRequested(
            call_id="visible",
            name="write_file",
            args={"path": "notes/draft.txt", "content": "text"},
            allow_text="Write a file in the workspace",
        )
    )
    qtbot.wait(10)
    card = window._approvals._cards["visible"]
    viewport = window._approvals_scroll.viewport()
    path_label = next(
        label for label in card.findChildren(QLabel) if label.text() == "notes/draft.txt"
    )
    for control in (card.summary_label, path_label, card.approve_btn, card.deny_btn):
        bounds = control.rect().translated(control.mapTo(viewport, control.rect().topLeft()))
        assert viewport.rect().contains(bounds), (control.accessibleName(), bounds, viewport.rect())
    assert window.size().toTuple() == size
    assert window._chat.height() >= 70
    assert window._input_edit.mapTo(window, window._input_edit.rect().bottomRight()).y() < size[1]


def test_local_diagnostics_render_numbers_without_claiming_provider_latency(
    window: MainWindow,
) -> None:
    window._on_event(
        DiagnosticsUpdate(
            audio={"input_queue_ms": 12.5},
            counters={"frames_skipped": 3},
            timings={"capture_encode": TimingSummary(8, 4.2, 7.6, 8.5, 6.4, 8.5)},
        )
    )
    text = window._diagnostics_label.text() + " " + window._diagnostics_label.toolTip()
    assert "12.5" in text
    assert "3" in text
    assert "local" in text.lower()
    assert "not provider" in text.lower()
    assert "hardware" in text.lower()
    assert "capture_encode" in text
    assert "p95 7.6" in text
    assert "8 samples" in text
    # The same event feeds the diagnostics page.
    assert "capture_encode" in str(window._diagnostics_page.report())


def test_diagnostics_page_opens_from_strip_and_menu_shortcut(window: MainWindow) -> None:
    assert not window._diagnostics_page.isVisible()
    window._strip.diagnostics_requested.emit()
    assert window._diagnostics_page.isVisible()
    window._diagnostics_page.hide()
    actions = {a.text(): a for a in window.menuBar().actions()}
    assert actions["&Diagnostics"].shortcut().toString() == "Ctrl+Shift+D"
    actions["&Diagnostics"].trigger()
    assert window._diagnostics_page.isVisible()
    window._diagnostics_page.hide()


def test_approvals_are_plain_text_inspectable_and_count_only_pending(
    window: MainWindow, qtbot: Any
) -> None:
    bridge = _RecordingBridge()
    commands = bridge.commands
    window._bridge = cast(Bridge, bridge)
    for index in range(3):
        window._on_event(
            ToolApprovalRequested(
                call_id=str(index),
                name="write_file",
                args={"path": "<draft>.txt", "content": "line " * 100},
                allow_text="Write a file in the workspace",
            )
        )
    assert window._approvals.pending_count() == 3
    qtbot.wait(10)
    assert window.height() == 600
    assert window._chat.height() >= 70
    assert window._input_edit.isVisible()
    card = window._approvals._cards["0"]
    labels = card.findChildren(QLabel)
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in labels)
    assert any("<draft>.txt" in label.text() and label.wordWrap() for label in labels)
    assert "line " * 10 in card.target_label.toolTip()
    assert not commands
    for call_id, button in (("0", "approve_btn"), ("1", "deny_btn"), ("2", "approve_btn")):
        control = getattr(window._approvals._cards[call_id], button)
        control.setFocus()
        qtbot.keyClick(control, Qt.Key.Key_Space)
    assert [type(command) for command in commands] == [ApproveTool, DenyTool, ApproveTool]
    assert window._approvals.pending_count() == 0
    # Approval can start a long-running tool; cancellation remains available until its result.
    assert card.cancel_btn.isEnabled()
    card.cancel_btn.click()
    assert isinstance(commands[-1], CancelTool)
    assert commands[-1].call_ids == ["0"]
    window._on_event(ToolResultSent(call_id="0", name="write_file", ok=True, result="saved"))
    window._on_event(ToolCallCancelled(call_ids=["1", "2"], reason="resolved"))
    assert window._approvals.pending_count() == 0
    window._bridge = None


def test_approving_via_rail_sends_command_then_shows_running(window: MainWindow) -> None:
    bridge = _RecordingBridge()
    window._bridge = cast(Bridge, bridge)
    window._on_event(
        ToolApprovalRequested(call_id="a", name="run_command", args={"command": "echo hi"})
    )
    card = window._approvals._cards["a"]
    card.approve_btn.click()
    assert bridge.commands == [ApproveTool(call_id="a")]
    assert card.status_label.text().startswith("Running")
    assert card.cancel_btn.isEnabled()
    window._bridge = None


def test_tool_rows_follow_call_lifecycle(window: MainWindow) -> None:
    window._on_event(ToolCallReceived(call_id="t", name="run_command", args={"command": "echo hi"}))
    window._on_event(
        ToolApprovalRequested(call_id="t", name="run_command", args={"command": "echo hi"})
    )
    assert window._chat.plain_text().count("run_command") == 1
    window._on_event(
        ToolResultSent(call_id="t", name="run_command", ok=True, result="hi\nsecond line")
    )
    text = window._chat.plain_text()
    assert "run_command echo hi · hi" in text
    assert "second line" not in text
    window._on_event(ToolCallReceived(call_id="u", name="read_file", args={"path": "a.txt"}))
    window._on_event(ToolCallCancelled(call_ids=["u"], reason="user"))
    assert "read_file a.txt · cancelled" in window._chat.plain_text()


def test_model_speaking_indicator_follows_stream(window: MainWindow) -> None:
    window._on_event(PartialTranscript("hello", TranscriptSource.MODEL, 1))
    assert not window._chat._footer.isHidden()
    window._on_event(TurnComplete(TranscriptSource.MODEL, "hello", 1))
    assert window._chat._footer.isHidden()


def test_typed_send_adds_user_bubble_and_copy_uses_chat_text(window: MainWindow) -> None:
    bridge = _RecordingBridge()
    window._bridge = cast(Bridge, bridge)
    window._input_edit.setText("typed question")
    window._on_send_clicked()
    assert len(bridge.commands) == 1
    assert "You: typed question" in window._chat.plain_text()
    window._copy_transcript_btn.click()
    assert QApplication.clipboard().text() == window._chat.plain_text()
    window._bridge = None


def test_strip_tracks_connection_usage_and_tier(window: MainWindow) -> None:
    assert "Free" in window._strip._model_label.text()
    window._on_event(ConnectionStateChanged(state=ConnectionState.LIVE))
    assert window._strip._state_chip.text() == "Live"
    window._on_event(
        UsageUpdate(prompt_tokens=1, completion_tokens=2, total_tokens=3, estimated_usd=0.5)
    )
    assert "$0.5000" in window._strip._cost.text()
    window._on_event(RateLimitHit(model="m", tier="tier1", tpm_used=1, tpm_limit=2))
    assert "Tier 1" in window._strip._model_label.text()


def test_meter_timer_only_runs_in_visible_active_voice_session(window: MainWindow) -> None:
    assert not window._meter_timer.isActive()
    window._bridge = cast(Bridge, _StubBridge())
    window._on_event(ConnectionStateChanged(state=ConnectionState.LIVE))
    assert not window._meter_timer.isActive()
    # The session mode is set when connecting; this fake changes it before a LIVE event.
    window._settings = window._settings.model_copy(update={"mode": "both"})
    window._on_event(ConnectionStateChanged(state=ConnectionState.LIVE))
    assert window._meter_timer.isActive()
    window.hide()
    assert not window._meter_timer.isActive()
    window.show()
    assert window._meter_timer.isActive()
    window.showMinimized()
    assert not window._meter_timer.isActive()
    window.showNormal()
    assert window._meter_timer.isActive()
    window._on_event(ConnectionStateChanged(state=ConnectionState.DISCONNECTED))
    assert not window._meter_timer.isActive()
    assert window._mic_meter.value() == 0
    window._bridge = None


def test_approved_tool_cancellation_survives_reconnect_until_bridge_finishes(
    window: MainWindow,
) -> None:
    bridge = _RecordingBridge()
    commands = bridge.commands
    window._bridge = cast(Bridge, bridge)
    for call_id in ("running-a", "running-b", "undecided"):
        window._on_event(
            ToolApprovalRequested(
                call_id=call_id,
                name="run_command",
                args={"command": "fake long-running task"},
            )
        )
    cards = window._approvals._cards
    for call_id in ("running-a", "running-b"):
        cards[call_id].approve_btn.click()
    window._on_event(ConnectionStateChanged(state=ConnectionState.DISCONNECTED))
    window._on_event(ConnectionStateChanged(state=ConnectionState.RECONNECTING))
    for call_id in ("running-a", "running-b"):
        assert cards[call_id].cancel_btn.isEnabled()
    assert not cards["undecided"].approve_btn.isEnabled()
    cards["running-a"].cancel_btn.click()
    assert isinstance(commands[-1], CancelTool)
    assert commands[-1].call_ids == ["running-a"]
    window._on_bridge_finished("stopped")
    assert not cards["running-b"].cancel_btn.isEnabled()
    assert "Session disconnected" in cards["running-b"].status_label.text()


@pytest.mark.parametrize("draft", ["", "a newer draft"])
def test_failed_screen_grounding_preserves_unsent_text_and_newer_draft(
    window: MainWindow, draft: str
) -> None:
    window._input_edit.setText(draft)
    window._on_event(TextNotSent(text="inspect this screen", reason="Fresh screen unavailable"))
    transcript = window._chat.plain_text()
    assert "[NOT SENT]" in transcript
    assert "inspect this screen" in transcript
    assert "Fresh screen unavailable" in transcript
    assert window._input_edit.text() == (draft or "inspect this screen")
    assert "inspect this screen" not in window._diagnostics_label.toolTip()


def test_disconnect_reason_counts_reconnects_except_user_request(window: MainWindow) -> None:
    window._on_event(DisconnectReason("user_request"))
    assert window._diagnostics_page.report()["summary"]["reconnects"] == 0
    window._on_event(DisconnectReason("go_away", "30s"))
    summary = window._diagnostics_page.report()["summary"]
    assert summary["reconnects"] == 1
    assert summary["last_disconnect_code"] == "go_away"


def test_mini_mode_toggles_and_mirrors_events(window: MainWindow) -> None:
    assert not window._pill.isVisible()
    window._toggle_mini_mode()
    assert window._pill.isVisible()
    window._on_event(
        ToolApprovalRequested(
            call_id="c1", name="run_command", args={"command": "pytest -q"}, allow_text=""
        )
    )
    assert window._pill.pending_count() == 1
    window._on_event(
        ToolResultSent(call_id="c1", name="run_command", ok=True, result="done", orphaned=False)
    )
    assert window._pill.pending_count() == 0
    window._on_event(PartialTranscript("hello there", TranscriptSource.MODEL, 1))
    assert "hello there" in window._pill_caption
    window._exit_mini_mode()
    assert not window._pill.isVisible()
