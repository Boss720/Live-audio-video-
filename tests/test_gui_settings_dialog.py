"""Settings dialog behaviour: round-trip, live/reconnect split, validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QCheckBox, QGroupBox, QLineEdit, QMessageBox
from pytestqt.qtbot import QtBot

from app.settings_dialog import LIVE_FIELDS, SettingsDialog
from settings.settings import AppSettings

FAKE_DEVICES: tuple[list[tuple[str, str | None]], list[tuple[str, str | None]]] = (
    [("Mic A", "Mic A"), ("Mic B", "Mic B")],
    [("Spk A", "Spk A")],
)


def _make(tmp_path: Path, **kw: Any) -> tuple[AppSettings, SettingsDialog]:
    settings = AppSettings(
        working_dir=tmp_path,
        workspace_root=tmp_path,
        api_key="legacy",
        api_key_tier1="tier1-key",
        api_key_free="free-key",
        debug=True,
        headless=False,
        **kw,
    )
    return settings, SettingsDialog(settings, devices_provider=lambda: FAKE_DEVICES)


def test_round_trip_all_fields(qtbot: QtBot, tmp_path: Path) -> None:
    settings, dlg = _make(
        tmp_path, input_device="Mic B", output_device="Spk A", echo_gate="on"
    )
    qtbot.addWidget(dlg)
    assert dlg.build_settings().model_dump() == settings.model_dump()
    assert dlg.changed_fields() == set()
    assert dlg.objectName() == "settingsDialog"
    assert dlg.windowTitle() == "Settings"


def test_round_trip_trigger_none_and_extended_model(qtbot: QtBot, tmp_path: Path) -> None:
    settings, dlg = _make(
        tmp_path,
        trigger_tokens=None,
        model="gemini-3.8-live-extended-thinking",
        fallback_model=None,
    )
    qtbot.addWidget(dlg)
    assert dlg.build_settings().model_dump() == settings.model_dump()


def test_live_vs_reconnect_counts(qtbot: QtBot, tmp_path: Path) -> None:
    _, dlg = _make(tmp_path)
    qtbot.addWidget(dlg)
    dlg._bindings["yolo"].set(True)
    assert dlg.changed_fields() == {"yolo"}
    assert "yolo" in LIVE_FIELDS
    assert "0 changes apply on reconnect · 1 applied live" in dlg._footer_label.text()
    dlg._bindings["model"].set("gemini-3.1-flash-live-preview")
    assert dlg.changed_fields() == {"yolo", "model"}
    assert "1 changes apply on reconnect · 1 applied live" in dlg._footer_label.text()


def test_trigger_tokens_none_checkbox(qtbot: QtBot, tmp_path: Path) -> None:
    _, dlg = _make(tmp_path)
    qtbot.addWidget(dlg)
    dlg._trigger_none_check.setChecked(True)
    assert dlg.build_settings().trigger_tokens is None
    assert "trigger_tokens" in dlg.changed_fields()


def test_aliases_parse(qtbot: QtBot, tmp_path: Path) -> None:
    _, dlg = _make(tmp_path)
    qtbot.addWidget(dlg)
    edit = dlg._bindings["wake_word_aliases"].widget
    assert isinstance(edit, QLineEdit)
    edit.setText(" a b ,, c,d ")
    assert dlg.build_settings().wake_word_aliases == ["a b", "c", "d"]


def test_api_keys_preserved(qtbot: QtBot, tmp_path: Path) -> None:
    _, dlg = _make(tmp_path)
    qtbot.addWidget(dlg)
    dlg._bindings["voice"].set("Kore")
    built = dlg.build_settings()
    assert (built.api_key, built.api_key_tier1, built.api_key_free) == (
        "legacy",
        "tier1-key",
        "free-key",
    )
    assert built.debug is True
    assert built.headless is False


def test_save_emits_signal(qtbot: QtBot, tmp_path: Path) -> None:
    _, dlg = _make(tmp_path)
    qtbot.addWidget(dlg)
    dlg._bindings["voice"].set("Kore")
    with qtbot.waitSignal(dlg.settings_saved) as blocker:
        dlg._save_btn.click()
    assert blocker.args is not None
    assert blocker.args[0].voice == "Kore"


def test_invalid_stays_open(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dlg = _make(tmp_path)
    qtbot.addWidget(dlg)
    warned: list[str] = []

    def fake_warning(*_a: Any, **_k: Any) -> QMessageBox.StandardButton:
        warned.append("w")
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", fake_warning)
    dlg._bindings["working_dir"].set(str(tmp_path / "missing"))
    dlg.show()
    dlg._save_btn.click()
    assert warned
    assert dlg.isVisible()


def test_risky_group(qtbot: QtBot, tmp_path: Path) -> None:
    _, dlg = _make(tmp_path)
    qtbot.addWidget(dlg)
    group = dlg.findChild(QGroupBox, "riskyGroup")
    assert group is not None
    texts = [c.text() for c in group.findChildren(QCheckBox)]
    assert any(t.startswith("Skip approvals (YOLO):") for t in texts)
    assert any(t.startswith("Computer control:") for t in texts)


def test_reset_section(qtbot: QtBot, tmp_path: Path) -> None:
    _, dlg = _make(tmp_path)
    qtbot.addWidget(dlg)
    dlg._bindings["voice"].set("Kore")
    dlg._nav.setCurrentRow(1)
    dlg.reset_current_section()
    assert dlg.build_settings().voice == "Puck"
