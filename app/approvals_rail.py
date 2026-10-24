"""Right-rail panel listing tool approval requests (Mock A).

Resolved cards are kept in place (greyed, controls disabled) and only the most
recent ``MAX_RESOLVED`` are retained; older resolved cards are deleted. There is
no separate "Recent" section. Only pending cards count toward the header.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.theme import (
    DANGER,
    GLASS_BORDER,
    GLASS_FILL,
    SUCCESS,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from core.events import ToolApprovalRequested, ToolCallCancelled, ToolResultSent

Risk = Literal["LOW", "MEDIUM", "HIGH"]

MEDIUM_COLOR = "#FBBF24"
MAX_RESOLVED = 20
MAX_ARG_CHARS = 160

_RISK_COLORS: dict[str, str] = {"HIGH": DANGER, "MEDIUM": MEDIUM_COLOR, "LOW": SUCCESS}

_SUMMARIES = {
    "write_file": "Write a file",
    "edit_file": "Edit a file",
    "read_file": "Read a file",
    "run_command": "Run a command",
    "list_directory": "List a directory",
    "computer_use": "Control the computer",
}

_DANGEROUS_COMMAND_MARKERS = (
    "remove-item",
    "rm ",
    "del ",
    "rmdir",
    "format",
    "-recurse",
)


def risk_reason(name: str, args: dict[str, Any]) -> str:
    """Short reason line for a dangerous command, or an empty string."""
    if name != "run_command":
        return ""
    command = str(args.get("command", "")).lower()
    if not any(marker in command for marker in _DANGEROUS_COMMAND_MARKERS):
        return ""
    if "remove-item" in command or "rm " in command or "del " in command or "rmdir" in command:
        return "recursive delete" if ("-recurse" in command or "-r" in command) else "deletes files"
    if "format" in command:
        return "may format a disk"
    return "recursive operation"


def risk_for(name: str, args: dict[str, Any]) -> Risk:
    """Classify a tool call by how much damage it could do."""
    if name in {"run_command", "delete_file", "computer_use"} or name.startswith("computer_"):
        return "HIGH"
    if name in {"write_file", "edit_file"}:
        return "MEDIUM"
    return "LOW"


def _arg_summary(args: dict[str, Any]) -> str:
    target = args.get("path") or args.get("command")
    if not target:
        return ""
    text = " ".join(str(target).split())
    if len(text) > MAX_ARG_CHARS:
        text = text[: MAX_ARG_CHARS - 1] + "…"
    return text


class _Card(QFrame):
    def __init__(self, event: ToolApprovalRequested, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.call_id = event.call_id
        self.name = event.name
        self.pending = True
        self.setObjectName("approvalCard")
        self.setAccessibleName(f"Tool approval request: {event.name}")
        self.risk: Risk = risk_for(event.name, event.args)
        self.reason = risk_reason(event.name, event.args)
        color = _RISK_COLORS[self.risk]
        self.setStyleSheet(
            f"QFrame#approvalCard {{ background: {GLASS_FILL}; border: 1px solid "
            f"{GLASS_BORDER}; border-radius: 10px; }}"
            f"QFrame#approvalCard QLabel {{ background: transparent; color: {TEXT_PRIMARY}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        top = QHBoxLayout()
        self.summary_label = QLabel(
            _SUMMARIES.get(event.name, event.name.replace("_", " ").capitalize())
        )
        self.risk_label = QLabel(self.risk)
        self.risk_label.setStyleSheet(f"color: {color}; font-weight: 700;")
        top.addWidget(self.summary_label, 1)
        top.addWidget(self.risk_label)
        layout.addLayout(top)

        self.target_label = QLabel(_arg_summary(event.args))
        self.target_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.target_label.setToolTip(json.dumps(event.args, ensure_ascii=False, indent=2))
        self.target_label.setVisible(bool(self.target_label.text()))
        layout.addWidget(self.target_label)

        self.reason_label = QLabel(self.reason)
        self.reason_label.setStyleSheet(f"color: {color};")
        self.reason_label.setVisible(bool(self.reason))
        layout.addWidget(self.reason_label)

        self.policy_label = QLabel(f"Policy: {event.allow_text}" if event.allow_text else "")
        self.policy_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self.policy_label.setVisible(bool(event.allow_text))
        layout.addWidget(self.policy_label)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)

        buttons = QHBoxLayout()
        self.approve_btn = QPushButton("Approve")
        self.approve_btn.setAccessibleName(f"Approve {event.name}")
        self.deny_btn = QPushButton("Deny")
        self.deny_btn.setAccessibleName(f"Deny {event.name}")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setAccessibleName(f"Cancel {event.name}")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.setEnabled(False)
        buttons.addWidget(self.approve_btn)
        buttons.addWidget(self.deny_btn)
        buttons.addWidget(self.cancel_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

    def set_result(self, message: str) -> None:
        self.pending = False
        self._set_status(message)
        self.approve_btn.setEnabled(False)
        self.deny_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)

    def set_approved(self) -> None:
        self.pending = False
        self._set_status("Running…")
        self.approve_btn.setEnabled(False)
        self.deny_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setVisible(True)


class ApprovalsRail(QFrame):
    """Vertical list of approval cards with a count header and "Deny all"."""

    approve_requested = Signal(str)
    deny_requested = Signal(str)
    cancel_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("approvalsRail")
        self._cards: dict[str, _Card] = {}
        self._resolved_order: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        self._header = QLabel("")
        self._header.setStyleSheet(f"color: {TEXT_PRIMARY}; font-weight: 700;")
        self._deny_all_btn = QPushButton("Deny all")
        self._deny_all_btn.setAccessibleName("Deny all pending approvals")
        self._deny_all_btn.clicked.connect(self._deny_all)
        header.addWidget(self._header, 1)
        header.addWidget(self._deny_all_btn)
        root.addLayout(header)

        self._cards_layout = QVBoxLayout()
        self._cards_layout.setSpacing(8)
        root.addLayout(self._cards_layout)
        root.addStretch(1)
        self._refresh_header()

    # -- public API --

    def add_request(self, event: ToolApprovalRequested) -> None:
        if event.call_id in self._cards:
            return
        card = _Card(event)
        cid = event.call_id
        card.approve_btn.clicked.connect(lambda _c=False, i=cid: self._on_approve(i))
        card.deny_btn.clicked.connect(lambda _c=False, i=cid: self._on_deny(i))
        card.cancel_btn.clicked.connect(lambda _c=False, i=cid: self._on_cancel(i))
        self._cards[cid] = card
        self._cards_layout.insertWidget(0, card)
        self._refresh_header()

    def mark_approved(self, call_id: str) -> None:
        card = self._cards.get(call_id)
        if card is not None:
            card.set_approved()
            self._refresh_header()

    def set_result(self, event: ToolResultSent) -> None:
        card = self._cards.get(event.call_id)
        if card is None:
            return
        message = f"{'OK' if event.ok else 'FAILED'}: {event.result}"
        if event.orphaned:
            message += " (orphaned)"
        self._resolve(card, message)

    def mark_cancelled(self, event: ToolCallCancelled) -> None:
        for call_id in event.call_ids:
            card = self._cards.get(call_id)
            if card is not None:
                self._resolve(card, f"Cancelled: {event.reason}")

    def resolve_all(self, message: str, *, session_finished: bool = False) -> None:
        # Approved tool tasks survive reconnects; only the supervisor's final
        # exit makes an in-flight tool impossible to cancel.
        for card in list(self._cards.values()):
            if card.pending or (session_finished and card.cancel_btn.isEnabled()):
                self._resolve(card, message)

    def pending_count(self) -> int:
        return sum(card.pending for card in self._cards.values())

    def has(self, call_id: str) -> bool:
        return call_id in self._cards

    # -- internals --

    def _resolve(self, card: _Card, message: str) -> None:
        card.set_result(message)
        if card.call_id in self._resolved_order:
            self._resolved_order.remove(card.call_id)
        self._resolved_order.append(card.call_id)
        while len(self._resolved_order) > MAX_RESOLVED:
            old = self._cards.pop(self._resolved_order.pop(0), None)
            if old is not None:
                self._cards_layout.removeWidget(old)
                old.deleteLater()
        self._refresh_header()

    def _refresh_header(self) -> None:
        count = self.pending_count()
        self._header.setText(f"Approvals · {count}")
        self._deny_all_btn.setEnabled(count > 0)

    def _on_approve(self, call_id: str) -> None:
        card = self._cards.get(call_id)
        if card is not None:
            card.approve_btn.setEnabled(False)
            card.deny_btn.setEnabled(False)
        self.approve_requested.emit(call_id)

    def _on_deny(self, call_id: str) -> None:
        card = self._cards.get(call_id)
        if card is not None and card.pending:
            self._resolve(card, "Denied · waiting for result")
        self.deny_requested.emit(call_id)

    def _on_cancel(self, call_id: str) -> None:
        card = self._cards.get(call_id)
        if card is not None:
            self._resolve(card, "Cancellation requested")
        self.cancel_requested.emit(call_id)

    def _deny_all(self) -> None:
        for call_id in [cid for cid, c in self._cards.items() if c.pending]:
            self._on_deny(call_id)
