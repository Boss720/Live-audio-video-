"""Wave 3: clipboard restore, key-combo guard, headless EOF, wake-word matching."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from typing import Any

import pytest

import headless
import tools.computer as computer
from core.wake_word import _soundex, contains_wake_word

# ----------------------------------------------------------------------
# computer.py
# ----------------------------------------------------------------------


class FakeGui:
    KEYBOARD_KEYS = [
        "ctrl", "alt", "shift", "win", "delete", "esc", "f4", "l", "r", "x",
        "w", "q", "s", "enter",
    ]  # fmt: skip

    class FailSafeException(Exception):
        pass

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def press(self, *a: Any) -> None:
        self.calls.append(("press", a))

    def hotkey(self, *a: Any) -> None:
        self.calls.append(("hotkey", a))

    def typewrite(self, *a: Any, **k: Any) -> None:
        self.calls.append(("typewrite", a))


class FakePyperclip:
    def __init__(self, content: Any = "old", paste_raises: bool = False) -> None:
        self.content = content
        self.paste_raises = paste_raises
        self.copy_raises_after = -1
        self.copies: list[str] = []

    def paste(self) -> Any:
        if self.paste_raises:
            raise RuntimeError("no clipboard")
        return self.content

    def copy(self, text: str) -> None:
        if 0 <= self.copy_raises_after <= len(self.copies):
            self.copies.append(text)
            raise RuntimeError("copy failed")
        self.copies.append(text)
        self.content = text


@pytest.fixture
def gui(monkeypatch: pytest.MonkeyPatch) -> FakeGui:
    fake = FakeGui()
    monkeypatch.setattr(computer, "_pyautogui", lambda: fake)
    monkeypatch.setattr(computer, "_own_window_is_foreground", lambda: False)
    monkeypatch.setattr(computer.time, "sleep", lambda s: None)
    return fake


def _install_clip(monkeypatch: pytest.MonkeyPatch, clip: FakePyperclip) -> None:
    monkeypatch.setitem(__import__("sys").modules, "pyperclip", clip)


def _type(text: str) -> Any:
    return computer.type_text(SimpleNamespace(text=text))  # type: ignore[arg-type]


def _key(keys: str) -> Any:
    return computer.key_press(SimpleNamespace(keys=keys))  # type: ignore[arg-type]


def test_paste_restores_previous_clipboard(
    gui: FakeGui, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = FakePyperclip("old")
    _install_clip(monkeypatch, clip)
    result = _type("héllo")
    assert result.ok
    assert clip.copies == ["héllo", "old"]
    assert clip.content == "old"
    assert ("hotkey", ("ctrl", "v")) in gui.calls


def test_paste_clears_clipboard_when_previous_unavailable(
    gui: FakeGui, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = FakePyperclip(paste_raises=True)
    _install_clip(monkeypatch, clip)
    assert _type("héllo").ok
    assert clip.content == ""


def test_paste_restore_failure_never_raises(
    gui: FakeGui, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = FakePyperclip("old")
    clip.copy_raises_after = 1  # second copy (the restore) fails
    _install_clip(monkeypatch, clip)
    assert _type("héllo").ok


@pytest.mark.parametrize(
    "combo",
    [
        "ctrl+alt+delete",
        "delete+alt+control",
        "Ctrl+Alt+Del",
        "win+l",
        "l+windows",
        "super+r",
        "cmd+x",
        "ctrl+shift+esc",
        "escape+shift+control",
    ],
)
def test_dangerous_combos_refused(gui: FakeGui, combo: str) -> None:
    result = _key(combo)
    assert not result.ok
    assert "Refused" in result.message
    assert gui.calls == []


@pytest.mark.parametrize("combo", ["alt+f4", "ctrl+w", "ctrl+q", "f4+alt"])
def test_close_combos_refused_only_for_own_window(
    gui: FakeGui, monkeypatch: pytest.MonkeyPatch, combo: str
) -> None:
    assert _key(combo).ok  # other app focused: allowed
    gui.calls.clear()
    monkeypatch.setattr(computer, "_own_window_is_foreground", lambda: True)
    result = _key(combo)
    assert not result.ok
    assert gui.calls == []


def test_ordinary_combo_still_works(gui: FakeGui) -> None:
    assert _key("ctrl+s").ok
    assert gui.calls == [("hotkey", ("ctrl", "s"))]


# ----------------------------------------------------------------------
# headless.py
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdin_thread_emits_eof_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    monkeypatch.setattr("sys.stdin", io.StringIO("hello\n"))
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    thread = headless._start_stdin_thread(loop, queue, threading.Event())
    assert await asyncio.wait_for(queue.get(), 2) == "hello"
    assert await asyncio.wait_for(queue.get(), 2) is headless._EOF
    thread.join(2)


@pytest.mark.asyncio
async def test_eof_triggers_disconnect() -> None:
    from core.commands import Disconnect

    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    event_queue: asyncio.Queue[Any] = asyncio.Queue()
    stdin_queue: asyncio.Queue[Any] = asyncio.Queue()

    async def session() -> Any:
        cmd = await command_queue.get()
        assert isinstance(cmd, Disconnect)
        return SimpleNamespace(code="user_request")

    task = asyncio.create_task(session())
    stdin_queue.put_nowait(headless._EOF)
    code = await asyncio.wait_for(
        headless._interactive_loop(
            SimpleNamespace(mode="text"),  # type: ignore[arg-type]
            command_queue,
            event_queue,
            stdin_queue,
            task,
        ),
        3,
    )
    assert code == 0


# ----------------------------------------------------------------------
# wake_word.py
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Leroy, open the file",
        "hey Lee Roy what's up",
        "ok Le Roi do it",
        "hey leeroy",
        "lee roi can you help",  # joined + phonetic
        "leroi",
    ],
)
def test_leroy_variants_match(text: str) -> None:
    assert contains_wake_word(text, "Leroy")


@pytest.mark.parametrize(
    "text",
    [
        "is it really true",
        "laura said hi",  # same Soundex as Leroy, but spelled too differently
        "roy is here",  # bare surname is not the wake word
        "the royal family",
    ],
)
def test_leroy_negatives(text: str) -> None:
    assert not contains_wake_word(text, "Leroy")


def test_whole_token_only() -> None:
    assert contains_wake_word("max open it", "max")
    assert not contains_wake_word("the maximum value", "max")
    assert not contains_wake_word("climaxes", "max")


def test_homophone_via_soundex() -> None:
    assert contains_wake_word("hey bonko", "Bongo")
    assert _soundex("Robert") == "R163"
    assert _soundex("Tymczak") == "T522"
    assert _soundex("Pfister") == "P236"
