"""Desktop input synthesis — mouse and keyboard control for the agent.

Tool arguments carry coordinates normalized to a 0-1000 grid on both axes
relative to the shared monitor (the convention the model is told in the
system prompt); they are mapped to physical pixels here using the monitor
geometry from ``media.screen``.

pyautogui is imported lazily inside each function so importing this module
never requires a display. FAILSAFE stays enabled: slamming the mouse into a
screen corner aborts the action and returns an error instead of
synthesizing input.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

from media import screen
from tools.registry import ToolResult

if TYPE_CHECKING:
    from tools.registry import (
        ComputerClickArgs,
        ComputerDragArgs,
        ComputerKeyArgs,
        ComputerMouseMoveArgs,
        ComputerScreenshotArgs,
        ComputerScrollArgs,
        ComputerTypeArgs,
    )

NORMALIZED_MAX = 1000
_TYPE_INTERVAL_SECONDS = 0.02
_DRAG_DURATION_SECONDS = 0.5
# pyautogui sleeps PAUSE seconds after every call (default 0.1), which taxes
# each computer-use action; keep a small margin instead so multi-step tasks
# do not accumulate seconds of dead time.
_ACTION_PAUSE_SECONDS = 0.02

# Monitor that normalized coordinates are mapped onto. Mirrors the
# ``screen_monitor`` setting; the session calls ``set_monitor`` at connect.
_monitor = 1

# Friendly key names -> pyautogui key names.
_KEY_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "option": "alt",
    "cmd": "win",
    "command": "win",
    "super": "win",
    "meta": "win",
    "windows": "win",
    "return": "enter",
    "escape": "esc",
    "spacebar": "space",
    "del": "delete",
    "bksp": "backspace",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "plus": "+",
}

_SCROLL_DIRECTIONS = ("up", "down", "left", "right")


def set_monitor(index: int) -> None:
    """Select the monitor that normalized coordinates are mapped onto."""
    global _monitor
    _monitor = index


def _pyautogui() -> Any:
    """Import pyautogui lazily, keep the corner failsafe on, cap its pause."""
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = _ACTION_PAUSE_SECONDS
    return pyautogui


def _to_physical(x: int, y: int) -> tuple[int, int]:
    """Map normalized 0-1000 coordinates to physical desktop pixels."""
    geo = screen.monitor_geometry(_monitor)
    clamped_x = min(max(x, 0), NORMALIZED_MAX)
    clamped_y = min(max(y, 0), NORMALIZED_MAX)
    px = geo["left"] + round(clamped_x / NORMALIZED_MAX * (geo["width"] - 1))
    py = geo["top"] + round(clamped_y / NORMALIZED_MAX * (geo["height"] - 1))
    return px, py


def _own_window_is_foreground() -> bool | None:
    """Whether the agent window has focus; None means Windows could not tell.

    Non-Windows platforms return False because there is no portable focus API.
    On Windows, a failed foreground lookup returns None so input can be refused.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        thread_id = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not thread_id or not pid.value:
            return None
        return int(pid.value) == os.getpid()
    except Exception:  # pragma: no cover - defensive, never block on this
        return None


def _self_input_guard() -> ToolResult | None:
    """Refuse to synthesize input while the agent's own window has focus.

    An app it meant to launch never came forward, so the keystrokes landed in
    this GUI instead. One of them was a space, the focused widget was the
    Disconnect button, and the session ended mid-word — reported as a random
    disconnect, with no error anywhere because it was a perfectly ordinary
    button press.

    Typing into its own controls is never the intent, and the failure is
    invisible from the model's side, so it is refused with an explanation it
    can act on rather than silently obeyed.
    """
    foreground = _own_window_is_foreground()
    if foreground is None:
        return ToolResult(
            False,
            "Could not verify the foreground window, so computer input was not sent. "
            "Focus another window and try again.",
        )
    if not foreground:
        return None
    return ToolResult(
        False,
        "Refused: the agent's own window is in the foreground, so this input "
        "would go into the agent's controls instead of the app you are aiming "
        "at. The application you expected probably did not come to the front. "
        "Take a screenshot, click the target window (or its taskbar button) to "
        "focus it, confirm from the screenshot that it is frontmost, then "
        "retry.",
    )


def _failsafe_result(exc: BaseException) -> ToolResult:
    return ToolResult(
        False,
        f"Action aborted by failsafe (mouse pushed into a screen corner): {exc}",
    )


def mouse_move(args: ComputerMouseMoveArgs, *, call_id: str = "") -> ToolResult:
    """Move the cursor to a normalized position."""
    gui = _pyautogui()
    px, py = _to_physical(args.x, args.y)
    try:
        gui.moveTo(px, py, duration=0.2)
    except gui.FailSafeException as exc:
        return _failsafe_result(exc)
    return ToolResult(True, f"Moved cursor to physical ({px}, {py}).")


def _click(args: ComputerClickArgs, button: str, clicks: int, label: str) -> ToolResult:
    refusal = _self_input_guard()
    if refusal is not None:
        return refusal
    gui = _pyautogui()
    px, py = _to_physical(args.x, args.y)
    try:
        gui.click(px, py, clicks=clicks, button=button)
    except gui.FailSafeException as exc:
        return _failsafe_result(exc)
    return ToolResult(True, f"{label} at physical ({px}, {py}).")


def left_click(args: ComputerClickArgs, *, call_id: str = "") -> ToolResult:
    """Left-click at a normalized position."""
    return _click(args, "left", 1, "Left-clicked")


def right_click(args: ComputerClickArgs, *, call_id: str = "") -> ToolResult:
    """Right-click at a normalized position."""
    return _click(args, "right", 1, "Right-clicked")


def double_click(args: ComputerClickArgs, *, call_id: str = "") -> ToolResult:
    """Double-click at a normalized position."""
    return _click(args, "left", 2, "Double-clicked")


def type_text(args: ComputerTypeArgs, *, call_id: str = "") -> ToolResult:
    """Type text into the currently focused field.

    ASCII goes through ``typewrite``; anything else is pasted via the
    clipboard because ``typewrite`` cannot synthesize non-ASCII characters.
    """
    refusal = _self_input_guard()
    if refusal is not None:
        return refusal
    gui = _pyautogui()
    try:
        if args.text.isascii():
            gui.typewrite(args.text, interval=_TYPE_INTERVAL_SECONDS)
            return ToolResult(True, f"Typed {len(args.text)} characters.")
        import pyperclip

        pyperclip.copy(args.text)
        gui.hotkey("ctrl", "v")
        return ToolResult(
            True,
            f"Pasted {len(args.text)} characters via the clipboard "
            "(non-ASCII text cannot be typed directly).",
        )
    except gui.FailSafeException as exc:
        return _failsafe_result(exc)


def _normalize_key(part: str, gui: Any) -> str | None:
    key = part.strip().lower()
    key = _KEY_ALIASES.get(key, key)
    return key if key in gui.KEYBOARD_KEYS else None


def key_press(args: ComputerKeyArgs, *, call_id: str = "") -> ToolResult:
    """Press a key or combination like ``enter`` or ``ctrl+s``."""
    refusal = _self_input_guard()
    if refusal is not None:
        return refusal
    gui = _pyautogui()
    parts = [p for p in args.keys.split("+") if p.strip()]
    keys: list[str] = []
    for part in parts:
        key = _normalize_key(part, gui)
        if key is None:
            return ToolResult(False, f"Unknown key name: '{part.strip()}'.")
        keys.append(key)
    if not keys:
        return ToolResult(False, "No keys given.")
    try:
        if len(keys) == 1:
            gui.press(keys[0])
        else:
            gui.hotkey(*keys)
    except gui.FailSafeException as exc:
        return _failsafe_result(exc)
    return ToolResult(True, f"Pressed {'+'.join(keys)}.")


def drag(args: ComputerDragArgs, *, call_id: str = "") -> ToolResult:
    """Drag with the left button from one normalized point to another."""
    refusal = _self_input_guard()
    if refusal is not None:
        return refusal
    gui = _pyautogui()
    px1, py1 = _to_physical(args.x1, args.y1)
    px2, py2 = _to_physical(args.x2, args.y2)
    try:
        gui.moveTo(px1, py1, duration=0.2)
        gui.dragTo(px2, py2, duration=_DRAG_DURATION_SECONDS, button="left")
    except gui.FailSafeException as exc:
        return _failsafe_result(exc)
    return ToolResult(True, f"Dragged from ({px1}, {py1}) to ({px2}, {py2}).")


def scroll(args: ComputerScrollArgs, *, call_id: str = "") -> ToolResult:
    """Scroll the mouse wheel, optionally at a normalized position."""
    refusal = _self_input_guard()
    if refusal is not None:
        return refusal
    gui = _pyautogui()
    direction = args.direction.strip().lower()
    if direction not in _SCROLL_DIRECTIONS:
        return ToolResult(
            False,
            f"Invalid direction '{args.direction}'. "
            f"Choose one of: {', '.join(_SCROLL_DIRECTIONS)}",
        )
    amount = max(1, min(args.amount, 100))
    position = ""
    try:
        if args.x is not None and args.y is not None:
            px, py = _to_physical(args.x, args.y)
            gui.moveTo(px, py, duration=0.2)
            position = f" at physical ({px}, {py})"
        if direction == "up":
            gui.scroll(amount)
        elif direction == "down":
            gui.scroll(-amount)
        elif direction == "left":
            gui.hscroll(-amount)
        else:
            gui.hscroll(amount)
    except gui.FailSafeException as exc:
        return _failsafe_result(exc)
    return ToolResult(True, f"Scrolled {direction} by {amount} clicks{position}.")


def take_screenshot(args: ComputerScreenshotArgs, *, call_id: str = "") -> ToolResult:
    """Request a fresh screenshot.

    No local side effect: the session pushes a fresh frame as realtime video
    input right after this tool responds.
    """
    return ToolResult(
        True, "Screenshot requested — a fresh frame of the screen follows."
    )
