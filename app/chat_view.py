"""Chat-style transcript view: bubbles, inline tool rows and system lines."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.theme import (
    DANGER,
    GLASS_BORDER,
    GLASS_FILL,
    GLASS_FILL_STRONG,
    SUCCESS,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from core.events import TranscriptSource

MAX_ITEMS = 500
_MONO = "Consolas, 'Cascadia Mono', monospace"
_SPEAKING_TEXT = "Model speaking… Esc to interrupt"


def _now() -> str:
    return time.strftime("%H:%M:%S")


@dataclass
class _Item:
    kind: str  # "bubble" | "tool" | "system"
    widget: QWidget
    source: TranscriptSource | None = None
    turn_id: int | None = None
    text: str = ""
    header: QLabel | None = None
    body: QLabel | None = None
    call_id: str = ""
    name: str = ""
    extra: dict[str, str] = field(default_factory=dict)


class ChatView(QScrollArea):
    """Scrollable transcript. Model text is always rendered as plain text."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatView")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._content.setObjectName("chatContent")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)

        self._footer = QLabel(_SPEAKING_TEXT)
        self._footer.setObjectName("chatSpeaking")
        self._footer.setStyleSheet(f"color: {TEXT_MUTED}; font-style: italic;")
        self._footer.setVisible(False)
        self._layout.addWidget(self._footer)
        self.setWidget(self._content)

        self._items: list[_Item] = []
        self._tools: dict[str, _Item] = {}
        self._open: dict[TranscriptSource, _Item] = {}

    # -- public API --

    def append_partial(
        self,
        text: str,
        source: TranscriptSource,
        turn_id: int,
        *,
        channel: str = "voice",
    ) -> None:
        stick = self._at_bottom()
        item = self._open.get(source)
        if item is None or item.turn_id != turn_id:
            item = self._new_bubble(source, turn_id, channel)
        item.text += text
        assert item.body is not None
        item.body.setText(item.text)
        self._after_add(stick)

    def complete_turn(
        self,
        source: TranscriptSource,
        text: str,
        turn_id: int,
        *,
        interrupted: bool = False,
    ) -> None:
        stick = self._at_bottom()
        item = self._open.get(source)
        if item is None or item.turn_id != turn_id:
            if not text:
                return
            item = self._new_bubble(source, turn_id, "voice")
        if text:
            item.text = text
        assert item.body is not None
        if interrupted:
            item.body.setText(f"{item.text} (interrupted)")
            item.extra["interrupted"] = "1"
        else:
            item.body.setText(item.text)
        self._open.pop(source, None)
        self._after_add(stick)

    def add_user_text(self, text: str) -> None:
        stick = self._at_bottom()
        item = self._new_bubble(TranscriptSource.USER, None, "typed")
        item.text = text
        assert item.body is not None
        item.body.setText(text)
        self._open.pop(TranscriptSource.USER, None)
        self._after_add(stick)

    def add_system(self, text: str) -> None:
        stick = self._at_bottom()
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._register(_Item("system", label, text=text))
        self._after_add(stick)

    def add_tool_row(self, call_id: str, name: str, detail: str) -> None:
        stick = self._at_bottom()
        label = QLabel()
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet(
            f"color: {TEXT_MUTED}; font-family: {_MONO}; font-size: 12px;"
            f" padding: 2px 8px; border-left: 2px solid {GLASS_BORDER};"
        )
        item = _Item("tool", label, call_id=call_id, name=name, body=label)
        item.extra["detail"] = detail
        label.setText(f"… {name} {detail}".rstrip())
        item.text = label.text()
        self._tools[call_id] = item
        self._register(item)
        self._after_add(stick)

    def update_tool_row(self, call_id: str, *, ok: bool, summary: str) -> None:
        item = self._tools.get(call_id)
        if item is None or item.body is None:
            return
        mark = "✓" if ok else "✗"
        colour = SUCCESS if ok else DANGER
        detail = item.extra.get("detail", "")
        head = f"{mark} {item.name} {detail}".rstrip()
        item.text = f"{head} · {summary}" if summary else head
        item.body.setText(item.text)
        item.body.setStyleSheet(
            f"color: {colour}; font-family: {_MONO}; font-size: 12px;"
            f" padding: 2px 8px; border-left: 2px solid {colour};"
        )

    def set_speaking(self, on: bool) -> None:
        stick = self._at_bottom()
        self._footer.setVisible(on)
        if on and stick:
            self._scroll_bottom_later()

    def plain_text(self) -> str:
        lines: list[str] = []
        for item in self._items:
            if item.kind == "bubble":
                who = "You" if item.source == TranscriptSource.USER else "Model"
                text = item.text
                if item.extra.get("interrupted"):
                    text += " (interrupted)"
                lines.append(f"{who}: {text}")
            elif item.text:
                lines.append(item.text)
        return "\n".join(lines) + ("\n" if lines else "")

    def clear(self) -> None:
        for item in self._items:
            self._remove_widget(item.widget)
        self._items.clear()
        self._tools.clear()
        self._open.clear()

    # -- internals --

    def _new_bubble(
        self, source: TranscriptSource, turn_id: int | None, channel: str
    ) -> _Item:
        is_user = source == TranscriptSource.USER
        frame = QFrame()
        frame.setObjectName("chatBubbleUser" if is_user else "chatBubbleModel")
        fill = GLASS_FILL_STRONG if is_user else GLASS_FILL
        frame.setStyleSheet(
            f"QFrame#{frame.objectName()} {{ background: {fill};"
            f" border: 1px solid {GLASS_BORDER}; border-radius: 12px; }}"
            " QLabel { background: transparent; border: none; }"
        )
        frame.setMaximumWidth(640)
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(12, 8, 12, 8)
        inner.setSpacing(4)

        who = "You" if is_user else "Model"
        suffix = f" · {channel}" if is_user else ""
        header = QLabel(f"{who} · {_now()}{suffix}")
        header.setTextFormat(Qt.TextFormat.PlainText)
        header.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        body = QLabel()
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 14px;")
        inner.addWidget(header)
        inner.addWidget(body)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        if is_user:
            row_layout.addStretch(1)
            row_layout.addWidget(frame)
        else:
            row_layout.addWidget(frame)
            row_layout.addStretch(1)

        item = _Item(
            "bubble",
            row,
            source=source,
            turn_id=turn_id,
            header=header,
            body=body,
        )
        self._open[source] = item
        self._register(item)
        return item

    def _register(self, item: _Item) -> None:
        # Insert before the trailing stretch/footer pair.
        self._layout.insertWidget(self._layout.count() - 2, item.widget)
        self._items.append(item)
        while len(self._items) > MAX_ITEMS:
            old = self._items.pop(0)
            self._forget(old)
            self._remove_widget(old.widget)

    def _forget(self, item: _Item) -> None:
        if item.call_id and self._tools.get(item.call_id) is item:
            del self._tools[item.call_id]
        if item.source is not None and self._open.get(item.source) is item:
            del self._open[item.source]

    def _remove_widget(self, widget: QWidget) -> None:
        self._layout.removeWidget(widget)
        widget.setParent(None)
        widget.deleteLater()

    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _after_add(self, stick: bool) -> None:
        if stick:
            self._scroll_bottom_later()

    def _scroll_bottom_later(self) -> None:
        self._scroll_now()
        QTimer.singleShot(0, self, self._scroll_now)

    def _scroll_now(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
