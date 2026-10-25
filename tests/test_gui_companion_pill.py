from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFrame, QLabel, QPushButton
from pytestqt.qtbot import QtBot

from app import companion_pill
from app.companion_pill import CompanionPill


@pytest.fixture
def pill(qtbot: QtBot) -> CompanionPill:
    widget = CompanionPill()
    qtbot.addWidget(widget)
    widget.show()
    return widget


def _btn(pill: CompanionPill, name: str) -> QPushButton:
    btn = pill.findChild(QPushButton, name)
    assert btn is not None
    return btn


def _label(pill: CompanionPill, name: str) -> QLabel:
    lbl = pill.findChild(QLabel, name)
    assert lbl is not None
    return lbl


def test_window_flags(pill: CompanionPill) -> None:
    flags = pill.windowFlags()
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.Tool
    assert pill.width() == companion_pill.PILL_WIDTH


def test_simple_signals(pill: CompanionPill, qtbot: QtBot) -> None:
    with qtbot.waitSignal(pill.stop_requested):
        _btn(pill, "pillStop").click()
    with qtbot.waitSignal(pill.open_requested):
        _btn(pill, "pillOpen").click()
    with qtbot.waitSignal(pill.screen_toggled) as blocker:
        _btn(pill, "pillScreen").click()
    assert blocker.args == [True]


def test_hold_press_release(pill: CompanionPill, qtbot: QtBot) -> None:
    hold = _btn(pill, "pillHold")
    with qtbot.waitSignal(pill.hold_pressed):
        QTest.mousePress(hold, Qt.MouseButton.LeftButton)
    with qtbot.waitSignal(pill.hold_released):
        QTest.mouseRelease(hold, Qt.MouseButton.LeftButton)


def test_hold_enabled(pill: CompanionPill) -> None:
    pill.set_hold_enabled(False)
    assert not _btn(pill, "pillHold").isEnabled()
    pill.set_hold_enabled(True)
    assert _btn(pill, "pillHold").isEnabled()


def test_set_screen_checked_no_signal(pill: CompanionPill, qtbot: QtBot) -> None:
    with qtbot.assertNotEmitted(pill.screen_toggled):
        pill.set_screen_checked(True)
    assert _btn(pill, "pillScreen").isChecked()


def test_pending_queue(pill: CompanionPill) -> None:
    strip = pill.findChild(QFrame, "pillPending")
    assert strip is not None
    text = _label(pill, "pillPendingText")
    assert pill.pending_count() == 0
    assert not strip.isVisible()
    pill.add_pending("a", "Run a command", "pytest -q")
    assert strip.isVisible()
    pill.add_pending("b", "Write a file", "x.py")
    pill.add_pending("b", "Write a file", "x.py")
    assert pill.pending_count() == 2
    assert "Run a command · pytest -q" in text.text()
    assert "+1" in text.text()
    pill.resolve_pending("a")
    assert "Write a file · x.py" in text.text()
    assert "+1" not in text.text()
    pill.resolve_pending("b")
    pill.resolve_pending("missing")
    assert pill.pending_count() == 0
    assert not strip.isVisible()


def test_allow_deny_emit_first_pending(pill: CompanionPill, qtbot: QtBot) -> None:
    pill.add_pending("a", "Run a command", "ls")
    pill.add_pending("b", "Run a command", "dir")
    with qtbot.waitSignal(pill.approve_requested) as allow:
        _btn(pill, "pillAllow").click()
    assert allow.args == ["a"]
    with qtbot.waitSignal(pill.deny_requested) as deny:
        _btn(pill, "pillDeny").click()
    assert deny.args == ["a"]


def test_toast_auto_hides(pill: CompanionPill, qtbot: QtBot) -> None:
    pill._toast_timer.setInterval(50)
    toast = _label(pill, "pillToast")
    assert not toast.isVisible()
    pill.show_toast("write_file tests/conftest.py · 3 lines")
    assert toast.isVisible()
    assert toast.text() == "write_file tests/conftest.py · 3 lines"
    qtbot.waitUntil(lambda: not toast.isVisible(), timeout=2000)


def test_level_clamped(pill: CompanionPill) -> None:
    orb = pill._orb
    pill.set_level(5.0)
    assert orb.level == 1.0
    pill.set_level(-2.0)
    assert orb.level == 0.0
    pill.set_level(0.4)
    assert orb.level == pytest.approx(0.4)


def test_caption_plain_text(pill: CompanionPill) -> None:
    pill.set_caption("<b>bold</b> & <i>x</i>")
    lbl = _label(pill, "pillCaptionText")
    assert lbl.textFormat() == Qt.TextFormat.PlainText
    assert lbl.text() == "<b>bold</b> & <i>x</i>"
    assert _label(pill, "pillCaption").textFormat() == Qt.TextFormat.PlainText
    assert _label(pill, "pillToast").textFormat() == Qt.TextFormat.PlainText


def test_caption_elided(pill: CompanionPill) -> None:
    pill.set_caption("word " * 200)
    assert len(_label(pill, "pillCaptionText").text()) < 200


def test_header_text(pill: CompanionPill) -> None:
    pill.set_state("Speaking")
    pill.set_model("3.8 Live")
    pill.set_latency_ms(540.0)
    assert _label(pill, "pillCaption").text() == "Speaking · 3.8 Live · 540 ms"
    pill.set_latency_ms(None)
    assert _label(pill, "pillCaption").text() == "Speaking · 3.8 Live"


def test_drag_moves_window(pill: CompanionPill) -> None:
    pill.move(100, 100)
    start = pill.frameGeometry().topLeft()
    none = Qt.KeyboardModifier.NoModifier
    QTest.mousePress(pill, Qt.MouseButton.LeftButton, none, QPoint(5, 5))
    QTest.mouseMove(pill, QPoint(45, 35))
    QTest.mouseRelease(pill, Qt.MouseButton.LeftButton, none, QPoint(45, 35))
    assert pill.frameGeometry().topLeft() != start

def test_interrupt_button(pill: CompanionPill, qtbot: QtBot) -> None:
    btn = _btn(pill, "pillInterrupt")
    assert btn.text() == "Stop"
    assert btn.toolTip() == "Stop talking (Esc)"
    assert not btn.isEnabled()
    pill.set_connected(True)
    assert btn.isEnabled()
    with qtbot.waitSignal(pill.interrupt_requested):
        btn.click()
    pill.set_connected(False)
    assert not btn.isEnabled()


def test_esc_emits_interrupt(pill: CompanionPill, qtbot: QtBot) -> None:
    pill.setFocus()
    with qtbot.waitSignal(pill.interrupt_requested):
        QTest.keyClick(pill, Qt.Key.Key_Escape)
    hold = _btn(pill, "pillHold")
    hold.setFocus()
    with qtbot.waitSignal(pill.interrupt_requested):
        QTest.keyClick(hold, Qt.Key.Key_Escape)


def test_stop_button_end_session(pill: CompanionPill, qtbot: QtBot) -> None:
    btn = _btn(pill, "pillStop")
    assert btn.toolTip() == "End session"
    with qtbot.waitSignal(pill.stop_requested):
        btn.click()


def test_pill_border(pill: CompanionPill) -> None:
    from PySide6.QtGui import QColor

    from app.theme import CYAN_400

    # The body and border are painted, not styled: a translucent top-level
    # window ignores stylesheet backgrounds.
    pill.resize(pill.sizeHint())
    image = pill.grab().toImage()
    mid = image.height() // 2
    border = image.pixelColor(0, mid)
    body = image.pixelColor(4, mid)
    cyan = QColor(CYAN_400)
    assert abs(border.green() - cyan.green()) < 40 and abs(border.blue() - cyan.blue()) < 40
    assert body.alpha() == 255
    assert body.blue() < 120  # dark navy, not see-through or white

