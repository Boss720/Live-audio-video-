"""Always-on-top companion pill (Mock B): orb, caption, quick actions, approvals."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.theme import (
    CYAN_400,
    DANGER,
    NAVY_700,
    NAVY_900,
    SUCCESS,
    TEXT_MUTED,
    TEXT_PRIMARY,
)

AMBER = "#FBBF24"
PILL_WIDTH = 330
ORB_SIZE = 52
TOAST_MS = 3000
CAPTION_TEXT_WIDTH = 210
CAPTION_LINES = 2
_BORDER = CYAN_400

_BUTTON_QSS = (
    "QPushButton {{ color: {fg}; background: rgba(148, 197, 255, 0.10);"
    " border: 1px solid rgba(125, 211, 252, 0.22); border-radius: 10px;"
    " padding: 6px 4px; font-size: 11.5px; }}"
    " QPushButton:hover {{ background: rgba(148, 197, 255, 0.20); }}"
    " QPushButton:checked {{ background: rgba(34, 211, 238, 0.28); }}"
    " QPushButton:disabled {{ color: {muted}; }}"
)


class _Orb(QWidget):
    """Cyan orb whose glow radius scales with speaker level."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pillOrb")
        self.setFixedSize(ORB_SIZE, ORB_SIZE)
        self._level = 0.0

    @property
    def level(self) -> float:
        return self._level

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, float(level)))
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        centre = QPointF(self.width() / 2, self.height() / 2)
        core = 15.0
        glow_r = core + 2.0 + self._level * (self.width() / 2 - core - 2.0)
        glow = QColor(CYAN_400)
        glow.setAlphaF(0.15 + 0.45 * self._level)
        clear = QColor(CYAN_400)
        clear.setAlphaF(0.0)
        halo = QRadialGradient(centre, glow_r)
        halo.setColorAt(0.0, glow)
        halo.setColorAt(1.0, clear)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(centre, glow_r, glow_r)
        body = QRadialGradient(QPointF(centre.x() - 5, centre.y() - 6), core * 1.6)
        body.setColorAt(0.0, QColor("#A5F3FC"))
        body.setColorAt(0.4, QColor(CYAN_400))
        body.setColorAt(1.0, QColor("#0E7490"))
        painter.setBrush(body)
        painter.drawEllipse(centre, core, core)
        painter.end()


class _Pending:
    __slots__ = ("call_id", "detail", "label")

    def __init__(self, call_id: str, label: str, detail: str) -> None:
        self.call_id = call_id
        self.label = label
        self.detail = detail


class CompanionPill(QFrame):
    hold_pressed = Signal()
    hold_released = Signal()
    screen_toggled = Signal(bool)
    stop_requested = Signal()
    interrupt_requested = Signal()
    open_requested = Signal()
    approve_requested = Signal(str)
    deny_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("companionPill")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(PILL_WIDTH)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # The body is painted in paintEvent: a translucent top-level window
        # does not draw stylesheet backgrounds, so the pill would be see-through.
        self.setStyleSheet(
            f"QFrame#companionPill QLabel {{ color: {TEXT_PRIMARY}; background: transparent; }}"
        )
        self._state = ""
        self._model = ""
        self._latency_ms: float | None = None
        self._caption = ""
        self._pending: list[_Pending] = []
        self._drag_offset: QPoint | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        self._orb = _Orb()
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self._caption_head = QLabel("")
        self._caption_head.setObjectName("pillCaption")
        self._caption_head.setStyleSheet("font-size: 12px; font-weight: 600;")
        self._caption_text = QLabel("")
        self._caption_text.setObjectName("pillCaptionText")
        self._caption_text.setWordWrap(True)
        self._caption_text.setStyleSheet(f"font-size: 12px; color: {TEXT_MUTED};")
        self._caption_text.setFixedWidth(CAPTION_TEXT_WIDTH)
        for lbl in (self._caption_head, self._caption_text):
            lbl.setTextFormat(Qt.TextFormat.PlainText)
        text_col.addWidget(self._caption_head)
        text_col.addWidget(self._caption_text)
        self._conn_dot = QLabel("●")
        self._conn_dot.setObjectName("pillConnection")
        self._conn_dot.setTextFormat(Qt.TextFormat.PlainText)
        top.addWidget(self._orb)
        top.addLayout(text_col, 1)
        top.addWidget(self._conn_dot, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(top)

        acts = QHBoxLayout()
        acts.setSpacing(6)
        self._hold = self._button("pillHold", "Hold", TEXT_PRIMARY)
        self._interrupt = self._button("pillInterrupt", "Stop", TEXT_PRIMARY)
        self._interrupt.setToolTip("Stop talking (Esc)")
        self._screen = self._button("pillScreen", "Screen", TEXT_PRIMARY)
        self._screen.setCheckable(True)
        self._stop = self._button("pillStop", "End", DANGER)
        self._stop.setToolTip("End session")
        self._open = self._button("pillOpen", "Open", TEXT_PRIMARY)
        for btn in (
            self._hold,
            self._interrupt,
            self._screen,
            self._stop,
            self._open,
        ):
            acts.addWidget(btn, 1)
        root.addLayout(acts)
        self._hold.pressed.connect(self.hold_pressed)
        self._hold.released.connect(self.hold_released)
        self._interrupt.clicked.connect(self.interrupt_requested)
        self._screen.toggled.connect(self.screen_toggled)
        self._stop.clicked.connect(self.stop_requested)
        self._open.clicked.connect(self.open_requested)

        self._pending_strip = QFrame()
        self._pending_strip.setObjectName("pillPending")
        self._pending_strip.setStyleSheet(
            "QFrame#pillPending { background: rgba(251, 191, 36, 0.10);"
            " border: 1px solid rgba(251, 191, 36, 0.40); border-radius: 12px; }"
            f" QFrame#pillPending QLabel {{ color: {AMBER}; font-size: 12px; }}"
        )
        strip = QHBoxLayout(self._pending_strip)
        strip.setContentsMargins(10, 7, 10, 7)
        strip.setSpacing(8)
        self._pending_label = QLabel("")
        self._pending_label.setObjectName("pillPendingText")
        self._pending_label.setTextFormat(Qt.TextFormat.PlainText)
        self._pending_label.setWordWrap(True)
        self._pending_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._allow = self._button("pillAllow", "Allow", SUCCESS)
        self._deny = self._button("pillDeny", "Deny", TEXT_PRIMARY)
        strip.addWidget(self._pending_label, 1)
        strip.addWidget(self._allow)
        strip.addWidget(self._deny)
        self._pending_strip.hide()
        root.addWidget(self._pending_strip)
        self._allow.clicked.connect(self._on_allow)
        self._deny.clicked.connect(self._on_deny)

        self._toast = QLabel("")
        self._toast.setObjectName("pillToast")
        self._toast.setTextFormat(Qt.TextFormat.PlainText)
        self._toast.setWordWrap(True)
        self._toast.setStyleSheet(
            f"QLabel#pillToast {{ color: {TEXT_PRIMARY}; background: rgba(6, 13, 31, 0.92);"
            f" border: 1px solid {_BORDER}; border-radius: 12px; padding: 9px 12px;"
            " font-size: 12px; }"
        )
        self._toast.hide()
        root.addWidget(self._toast)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(TOAST_MS)
        self._toast_timer.timeout.connect(self._toast.hide)

        self.set_connected(False)
        self._refresh_head()

    def _button(self, name: str, text: str, fg: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName(name)
        btn.setStyleSheet(_BUTTON_QSS.format(fg=fg, muted=TEXT_MUTED))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    # -- public API --

    def set_state(self, text: str) -> None:
        self._state = text
        self._refresh_head()

    def set_model(self, text: str) -> None:
        self._model = text
        self._refresh_head()

    def set_latency_ms(self, ms: float | None) -> None:
        self._latency_ms = ms
        self._refresh_head()

    def set_caption(self, text: str) -> None:
        self._caption = text
        flat = " ".join(text.split())
        metrics = self._caption_text.fontMetrics()
        budget = CAPTION_TEXT_WIDTH * CAPTION_LINES - 2 * metrics.averageCharWidth()
        self._caption_text.setText(metrics.elidedText(flat, Qt.TextElideMode.ElideRight, budget))
        self._caption_text.setToolTip(text)
        self._caption_text.setVisible(bool(flat))

    def set_level(self, level: float) -> None:
        self._orb.set_level(level)

    def set_connected(self, connected: bool) -> None:
        colour = SUCCESS if connected else TEXT_MUTED
        self._conn_dot.setStyleSheet(f"color: {colour}; font-size: 10px;")
        self._conn_dot.setToolTip("Connected" if connected else "Disconnected")
        self._interrupt.setEnabled(connected)

    def set_screen_checked(self, checked: bool) -> None:
        was = self._screen.blockSignals(True)
        self._screen.setChecked(checked)
        self._screen.blockSignals(was)

    def add_pending(self, call_id: str, label: str, detail: str) -> None:
        if any(p.call_id == call_id for p in self._pending):
            return
        self._pending.append(_Pending(call_id, label, detail))
        self._refresh_pending()

    def resolve_pending(self, call_id: str) -> None:
        before = len(self._pending)
        self._pending = [p for p in self._pending if p.call_id != call_id]
        if len(self._pending) != before:
            self._refresh_pending()

    def pending_count(self) -> int:
        return len(self._pending)

    def show_toast(self, text: str) -> None:
        self._toast.setText(text)
        self._toast.show()
        self._toast_timer.start()

    def set_hold_enabled(self, enabled: bool) -> None:
        self._hold.setEnabled(enabled)

    # -- internals --

    def _refresh_head(self) -> None:
        parts = [p for p in (self._state, self._model) if p]
        if self._latency_ms is not None:
            parts.append(f"{self._latency_ms:.0f} ms")
        self._caption_head.setText(" · ".join(parts))

    def _refresh_pending(self) -> None:
        if not self._pending:
            self._pending_strip.hide()
            self._pending_label.setText("")
            return
        first = self._pending[0]
        text = first.label if not first.detail else f"{first.label} · {first.detail}"
        extra = len(self._pending) - 1
        if extra > 0:
            text += f"  +{extra}"
        self._pending_label.setText(text)
        self._pending_label.setToolTip(text)
        self._pending_strip.show()

    def _on_allow(self) -> None:
        if self._pending:
            self.approve_requested.emit(self._pending[0].call_id)

    def _on_deny(self) -> None:
        if self._pending:
            self.deny_requested.emit(self._pending[0].call_id)

    # -- painting --

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Half-pixel inset keeps the 1 px border crisp on the pixel grid.
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, QColor(NAVY_900))
        gradient.setColorAt(1, QColor(NAVY_700))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(_BORDER), 1))
        painter.drawRoundedRect(rect, 22, 22)
        painter.end()

    # -- keyboard / drag --

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.interrupt_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)
