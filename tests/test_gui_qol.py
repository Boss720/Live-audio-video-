"""Behavioral desktop checks with no provider, microphone, or screen capture."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QLabel

from app.bridge import Bridge
from app.main_window import MainWindow
from app.theme import MAIN_QSS
from core.commands import ApproveTool, CancelTool, DenyTool
from core.events import (
    ConnectionState,
    ConnectionStateChanged,
    DiagnosticsUpdate,
    TextNotSent,
    ToolApprovalRequested,
    ToolCallCancelled,
    ToolResultSent,
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
    result.resize(720, 520)
    result.show()
    qtbot.waitExposed(result)
    yield result
    result._bridge = None


def test_compact_settings_keep_conversation_and_composer_usable(window: MainWindow) -> None:
    assert window.height() == 520
    assert window._transcript.height() >= 70
    assert window._input_edit.isVisible()
    assert window._input_edit.mapTo(window, window._input_edit.rect().bottomRight()).y() < 520
    scroll = window._settings_scroll
    assert scroll.verticalScrollBar().maximum() > 0
    scroll.ensureWidgetVisible(window._save_settings_btn)
    assert (
        scroll.viewport()
        .rect()
        .intersects(
            window._save_settings_btn.rect().translated(
                window._save_settings_btn.mapTo(
                    scroll.viewport(), window._save_settings_btn.rect().topLeft()
                )
            )
        )
    )
    assert window._connect_btn.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_settings_saved_and_next_connect_state_roundtrip(window: MainWindow) -> None:
    assert "saved" in window._settings_state_label.text().lower()
    window._audio_chunk_combo.setCurrentIndex(window._audio_chunk_combo.findData(20))
    window._screen_deduplicate_check.setChecked(True)
    assert "unsaved" in window._settings_state_label.text().lower()
    window._on_save_settings()
    assert "saved" in window._settings_state_label.text().lower()
    assert "next connect" in window._settings_state_label.text().lower()
    loaded = AppSettings.load()
    assert loaded.audio_chunk_duration_ms == 20
    assert loaded.screen_deduplicate is True
    window._system_prompt_edit.setPlainText("next session instruction")
    assert "unsaved" in window._settings_state_label.text().lower()


@pytest.mark.parametrize("size", [(720, 520), (1200, 800)])
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
    card = window._tool_cards["visible"]
    viewport = window._tool_scroll.viewport()
    path_label = next(
        label for label in card.findChildren(QLabel) if label.text() == "notes/draft.txt"
    )
    for control in (card._summary, path_label, card._approve_btn, card._deny_btn, card._cancel_btn):
        bounds = control.rect().translated(control.mapTo(viewport, control.rect().topLeft()))
        assert viewport.rect().contains(bounds), (control.accessibleName(), bounds, viewport.rect())
    assert window.size().toTuple() == size
    assert window._transcript.height() >= 70
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


def test_approvals_are_plain_text_inspectable_and_count_only_pending(
    window: MainWindow, qtbot: Any
) -> None:
    commands: list[Any] = []

    class Stub(_StubBridge):
        def send_command(self, command: Any) -> None:
            commands.append(command)

    window._bridge = cast(Bridge, Stub())
    for index in range(3):
        window._on_event(
            ToolApprovalRequested(
                call_id=str(index),
                name="write_file",
                args={"path": "<draft>.txt", "content": "line " * 100},
                allow_text="Write a file in the workspace",
            )
        )
    assert "3 pending" in window._approval_count_label.text().lower()
    qtbot.wait(10)
    assert window.height() == 520
    assert window._transcript.height() >= 70
    assert window._input_edit.isVisible()
    card = window._tool_cards["0"]
    labels = card.findChildren(QLabel)
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in labels)
    assert any("<draft>.txt" in label.text() and label.wordWrap() for label in labels)
    assert card._details.toPlainText().find("line " * 10) >= 0
    assert not commands
    for index, button in enumerate(("_approve_btn", "_deny_btn", "_cancel_btn")):
        control = getattr(window._tool_cards[str(index)], button)
        control.setFocus()
        qtbot.keyClick(control, Qt.Key.Key_Space)
    assert [type(command) for command in commands] == [ApproveTool, DenyTool, CancelTool]
    assert "0 pending" in window._approval_count_label.text().lower()
    # Approval can start a long-running tool; cancellation remains available until its result.
    assert card._cancel_btn.isEnabled()
    card._cancel_btn.click()
    assert isinstance(commands[-1], CancelTool)
    assert commands[-1].call_ids == ["0"]
    window._on_event(ToolResultSent(call_id="0", name="write_file", ok=True, result="saved"))
    window._on_event(ToolCallCancelled(call_ids=["1", "2"], reason="resolved"))
    assert "0 pending" in window._approval_count_label.text().lower()
    window._bridge = None


def test_meter_timer_only_runs_in_visible_active_voice_session(window: MainWindow) -> None:
    assert not window._meter_timer.isActive()
    window._bridge = cast(Bridge, _StubBridge())
    window._on_event(ConnectionStateChanged(state=ConnectionState.LIVE))
    assert not window._meter_timer.isActive()
    # The session mode is set when connecting; this fake changes it before a LIVE event.
    window._mode_combo.setCurrentText("Both")
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
    commands: list[Any] = []

    class Stub(_StubBridge):
        def send_command(self, command: Any) -> None:
            commands.append(command)

    window._bridge = cast(Bridge, Stub())
    for call_id in ("running-a", "running-b", "undecided"):
        window._on_event(
            ToolApprovalRequested(
                call_id=call_id,
                name="run_command",
                args={"command": "fake long-running task"},
            )
        )
    for call_id in ("running-a", "running-b"):
        window._tool_cards[call_id]._approve_btn.click()
    window._on_event(ConnectionStateChanged(state=ConnectionState.DISCONNECTED))
    window._on_event(ConnectionStateChanged(state=ConnectionState.RECONNECTING))
    for call_id in ("running-a", "running-b"):
        assert window._tool_cards[call_id]._cancel_btn.isEnabled()
    assert not window._tool_cards["undecided"]._approve_btn.isEnabled()
    window._tool_cards["running-a"]._cancel_btn.click()
    assert isinstance(commands[-1], CancelTool)
    assert commands[-1].call_ids == ["running-a"]
    window._on_bridge_finished("stopped")
    assert not window._tool_cards["running-b"]._cancel_btn.isEnabled()
    assert "Session disconnected" in window._tool_cards["running-b"]._status.text()


@pytest.mark.parametrize("draft", ["", "a newer draft"])
def test_failed_screen_grounding_preserves_unsent_text_and_newer_draft(
    window: MainWindow, draft: str
) -> None:
    window._input_edit.setText(draft)
    window._on_event(TextNotSent(text="inspect this screen", reason="Fresh screen unavailable"))
    transcript = window._transcript.toPlainText()
    assert "[NOT SENT]" in transcript
    assert "inspect this screen" in transcript
    assert "Fresh screen unavailable" in transcript
    assert window._input_edit.text() == (draft or "inspect this screen")
    assert "inspect this screen" not in window._diagnostics_label.toolTip()
