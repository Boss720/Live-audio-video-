from __future__ import annotations

from typing import Any

import pytest

from app.approvals_rail import ApprovalsRail, risk_for, risk_reason
from core.events import ToolApprovalRequested, ToolCallCancelled, ToolResultSent


def _req(call_id: str, name: str, **args: Any) -> ToolApprovalRequested:
    return ToolApprovalRequested(call_id=call_id, name=name, args=args)


@pytest.mark.parametrize(
    ("name", "args", "risk"),
    [
        ("run_command", {"command": "dir"}, "HIGH"),
        ("delete_file", {"path": "a"}, "HIGH"),
        ("computer_use", {}, "HIGH"),
        ("computer_left_click", {}, "HIGH"),
        ("write_file", {"path": "a"}, "MEDIUM"),
        ("edit_file", {"path": "a"}, "MEDIUM"),
        ("read_file", {"path": "a"}, "LOW"),
        ("list_directory", {}, "LOW"),
    ],
)
def test_risk_for(name: str, args: dict[str, Any], risk: str) -> None:
    assert risk_for(name, args) == risk


def test_risk_reason_recursive_delete() -> None:
    assert risk_reason("run_command", {"command": r"Remove-Item -Recurse .\build"}) == (
        "recursive delete"
    )
    assert risk_reason("run_command", {"command": "echo hi"}) == ""
    assert risk_reason("write_file", {"path": "x"}) == ""


def test_add_approve_result_flow(qtbot: Any) -> None:
    rail = ApprovalsRail()
    qtbot.addWidget(rail)
    assert rail.objectName() == "approvalsRail"
    rail.add_request(_req("c1", "write_file", path="tests/conftest.py"))
    assert rail.has("c1")
    assert rail.pending_count() == 1
    assert rail._header.text() == "Approvals · 1"
    card = rail._cards["c1"]
    assert card.risk_label.text() == "MEDIUM"
    with qtbot.waitSignal(rail.approve_requested) as sig:
        card.approve_btn.click()
    assert sig.args == ["c1"]
    rail.mark_approved("c1")
    assert card.status_label.text() == "Running…"
    assert card.cancel_btn.isEnabled()
    assert not card.approve_btn.isEnabled()
    assert not card.deny_btn.isEnabled()
    assert rail.pending_count() == 0
    rail.set_result(ToolResultSent(call_id="c1", name="write_file", ok=True, result="done"))
    assert card.status_label.text() == "OK: done"
    assert not card.cancel_btn.isEnabled()
    rail.set_result(ToolResultSent(call_id="zz", name="x", ok=False, result="r"))


def test_args_truncated_and_tooltip(qtbot: Any) -> None:
    rail = ApprovalsRail()
    qtbot.addWidget(rail)
    rail.add_request(_req("c1", "run_command", command="x" * 500))
    card = rail._cards["c1"]
    assert len(card.target_label.text()) == 160
    assert "500" not in card.target_label.text()
    assert "x" * 500 in card.target_label.toolTip()


def test_high_risk_reason_shown(qtbot: Any) -> None:
    rail = ApprovalsRail()
    qtbot.addWidget(rail)
    rail.add_request(_req("c1", "run_command", command=r"Remove-Item -Recurse .\build"))
    card = rail._cards["c1"]
    assert card.risk_label.text() == "HIGH"
    assert card.reason_label.text() == "recursive delete"


def test_deny_all_emits_per_pending(qtbot: Any) -> None:
    rail = ApprovalsRail()
    qtbot.addWidget(rail)
    rail.add_request(_req("a", "write_file", path="p"))
    rail.add_request(_req("b", "run_command", command="ls"))
    rail.add_request(_req("c", "read_file", path="p"))
    rail.mark_approved("c")
    seen: list[str] = []
    rail.deny_requested.connect(seen.append)
    rail._deny_all_btn.click()
    assert sorted(seen) == ["a", "b"]
    assert rail.pending_count() == 0
    assert not rail._deny_all_btn.isEnabled()


def test_cancel_signal(qtbot: Any) -> None:
    rail = ApprovalsRail()
    qtbot.addWidget(rail)
    rail.add_request(_req("a", "run_command", command="ls"))
    rail.mark_approved("a")
    with qtbot.waitSignal(rail.cancel_requested) as sig:
        rail._cards["a"].cancel_btn.click()
    assert sig.args == ["a"]
    assert rail._cards["a"].status_label.text() == "Cancellation requested"


def test_mark_cancelled(qtbot: Any) -> None:
    rail = ApprovalsRail()
    qtbot.addWidget(rail)
    rail.add_request(_req("a", "write_file", path="p"))
    rail.mark_cancelled(ToolCallCancelled(call_ids=["a", "nope"], reason="user"))
    assert rail._cards["a"].status_label.text() == "Cancelled: user"
    assert rail.pending_count() == 0


def test_resolve_all(qtbot: Any) -> None:
    rail = ApprovalsRail()
    qtbot.addWidget(rail)
    rail.add_request(_req("p", "write_file", path="p"))
    rail.add_request(_req("r", "run_command", command="ls"))
    rail.mark_approved("r")
    rail.resolve_all("Reconnected")
    assert rail._cards["p"].status_label.text() == "Reconnected"
    assert rail._cards["r"].status_label.text() == "Running…"
    rail.resolve_all("Session ended", session_finished=True)
    assert rail._cards["r"].status_label.text() == "Session ended"
    assert rail.pending_count() == 0


def test_resolved_cards_capped(qtbot: Any) -> None:
    rail = ApprovalsRail()
    qtbot.addWidget(rail)
    for i in range(25):
        rail.add_request(_req(f"c{i}", "read_file", path="p"))
        rail.set_result(ToolResultSent(call_id=f"c{i}", name="read_file", ok=True, result="r"))
    assert not rail.has("c0")
    assert rail.has("c24")
    assert len(rail._cards) == 20
