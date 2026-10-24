from __future__ import annotations

from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from app.chat_view import ChatView
from core.events import TranscriptSource

U = TranscriptSource.USER
M = TranscriptSource.MODEL


def _make(qtbot: QtBot) -> ChatView:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(500, 400)
    return view


def test_object_name(qtbot: QtBot) -> None:
    assert _make(qtbot).objectName() == "chatView"


def test_partials_merge_into_one_bubble(qtbot: QtBot) -> None:
    v = _make(qtbot)
    v.append_partial("Hel", M, 1)
    v.append_partial("lo", M, 1)
    assert v.plain_text() == "Model: Hello\n"
    assert len(v._items) == 1


def test_new_turn_id_new_bubble(qtbot: QtBot) -> None:
    v = _make(qtbot)
    v.append_partial("a", M, 1)
    v.append_partial("b", M, 2)
    assert v.plain_text() == "Model: a\nModel: b\n"


def test_complete_replaces_text(qtbot: QtBot) -> None:
    v = _make(qtbot)
    v.append_partial("drft", M, 1)
    v.complete_turn(M, "final", 1)
    assert v.plain_text() == "Model: final\n"
    assert len(v._items) == 1
    v.append_partial("next", M, 1)
    assert len(v._items) == 2


def test_interrupted_marker(qtbot: QtBot) -> None:
    v = _make(qtbot)
    v.append_partial("half", M, 1)
    v.complete_turn(M, "", 1, interrupted=True)
    assert "(interrupted)" in v.plain_text()
    body = v._items[0].body
    assert body is not None
    assert body.text().endswith("(interrupted)")


def test_tool_row_ok_fail_unknown(qtbot: QtBot) -> None:
    v = _make(qtbot)
    v.add_tool_row("a", "run_command", "pytest")
    v.add_tool_row("b", "read_file", "x.py")
    a = v._tools["a"].body
    b = v._tools["b"].body
    assert a is not None
    assert b is not None
    assert a.text().startswith("… run_command")
    v.update_tool_row("a", ok=True, summary="exit 0")
    v.update_tool_row("b", ok=False, summary="denied")
    v.update_tool_row("zzz", ok=True, summary="ignored")
    assert a.text().startswith("✓ run_command pytest")
    assert "exit 0" in a.text()
    assert b.text().startswith("✗")
    assert len(v._items) == 2


def test_plain_text_order(qtbot: QtBot) -> None:
    v = _make(qtbot)
    v.add_user_text("hi")
    v.append_partial("yo", M, 1)
    v.complete_turn(M, "yo", 1)
    v.append_partial("voice", U, 2)
    assert v.plain_text() == "You: hi\nModel: yo\nYou: voice\n"


def test_typed_header(qtbot: QtBot) -> None:
    v = _make(qtbot)
    v.add_user_text("hi")
    header = v._items[0].header
    assert header is not None
    assert header.text().startswith("You · ")
    assert header.text().endswith("· typed")


def test_clear(qtbot: QtBot) -> None:
    v = _make(qtbot)
    v.add_user_text("hi")
    v.add_tool_row("a", "n", "d")
    v.add_system("sys")
    v.clear()
    assert v.plain_text() == ""
    assert v._items == []
    v.update_tool_row("a", ok=True, summary="x")


def test_html_literal(qtbot: QtBot) -> None:
    v = _make(qtbot)
    v.append_partial("<b>x</b>", M, 1)
    body = v._items[0].body
    assert body is not None
    assert body.text() == "<b>x</b>"
    assert body.textFormat() == Qt.TextFormat.PlainText


def test_speaking_footer(qtbot: QtBot) -> None:
    v = _make(qtbot)
    v.show()
    v.set_speaking(True)
    assert v._footer.isVisible()
    v.set_speaking(False)
    assert not v._footer.isVisible()


def test_cap_500(qtbot: QtBot) -> None:
    v = _make(qtbot)
    for i in range(510):
        v.add_system(f"s{i}")
    assert len(v._items) == 500
    assert v.plain_text().startswith("s10\n")
