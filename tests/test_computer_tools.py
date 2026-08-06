"""Tests for the computer-use tools (mouse/keyboard control)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Generator
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from app.main_window import MainWindow
from audit.journal import Journal
from core.commands import GatingMode
from core.dispatcher import ToolDispatcher
from core.events import ToolApprovalRequested, ToolResultSent
from policy import PolicyEngine
from policy.engine import DecisionKind
from settings.settings import AppSettings
from tools import computer
from tools._state import set_policy_engine, set_workspace_root
from tools.registry import (
    ALL_TOOLS,
    COMPUTER_TOOL_NAMES,
    COMPUTER_TOOLS,
    ComputerClickArgs,
    ComputerKeyArgs,
    ComputerMouseMoveArgs,
    ComputerScrollArgs,
    ComputerTypeArgs,
    ToolResult,
    build_function_declarations,
)

# -- Fakes --


class _FakeFailSafeException(Exception):
    pass


class _FakePyAutoGUI:
    """Records calls instead of synthesizing input."""

    FailSafeException = _FakeFailSafeException
    FAILSAFE = True
    KEYBOARD_KEYS = [
        "ctrl", "alt", "shift", "win", "enter", "tab", "space", "esc",
        "delete", "backspace", "pageup", "pagedown", "up", "down", "left",
        "right", "s", "v", "f5", "+",
    ]

    def __init__(self, *, failsafe: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._failsafe = failsafe

    def _record(self, *call: Any) -> None:
        if self._failsafe:
            raise _FakeFailSafeException("failsafe triggered")
        self.calls.append(call)

    def moveTo(self, x: int, y: int, duration: float = 0.0) -> None:
        self._record("moveTo", x, y)

    def click(self, x: int, y: int, clicks: int = 1, button: str = "left") -> None:
        self._record("click", x, y, clicks, button)

    def typewrite(self, text: str, interval: float = 0.0) -> None:
        self._record("typewrite", text)

    def press(self, key: str) -> None:
        self._record("press", key)

    def hotkey(self, *keys: str) -> None:
        self._record("hotkey", *keys)

    def dragTo(self, x: int, y: int, duration: float = 0.0, button: str = "left") -> None:
        self._record("dragTo", x, y, button)

    def scroll(self, clicks: int) -> None:
        self._record("scroll", clicks)

    def hscroll(self, clicks: int) -> None:
        self._record("hscroll", clicks)


FAKE_GEOMETRY = {"left": 100, "top": 50, "width": 1921, "height": 1081}


@pytest.fixture
def fake_gui(monkeypatch: Any) -> _FakePyAutoGUI:
    fake = _FakePyAutoGUI()
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    monkeypatch.setattr(
        "media.screen.monitor_geometry", lambda monitor=1: FAKE_GEOMETRY
    )
    monkeypatch.setattr(computer, "_monitor", 1)
    return fake


# -- Coordinate mapping --


def test_to_physical_maps_normalized_center(fake_gui: _FakePyAutoGUI) -> None:
    # (500, 500) maps to the center of the 1921x1081 fake monitor, offset by
    # its (100, 50) origin.
    assert computer._to_physical(500, 500) == (100 + 960, 50 + 540)


def test_to_physical_clamps_out_of_range(fake_gui: _FakePyAutoGUI) -> None:
    assert computer._to_physical(0, 1000) == (100, 50 + 1080)
    assert computer._to_physical(-5, 1200) == (100, 50 + 1080)


# -- Executor actions --


def test_mouse_move(fake_gui: _FakePyAutoGUI) -> None:
    result = computer.mouse_move(ComputerMouseMoveArgs(x=500, y=500))
    assert result.ok is True
    assert fake_gui.calls == [("moveTo", 1060, 590)]


def test_click_variants(fake_gui: _FakePyAutoGUI) -> None:
    args = ComputerClickArgs(x=250, y=750)
    assert computer.left_click(args).ok is True
    assert computer.right_click(args).ok is True
    assert computer.double_click(args).ok is True
    px, py = 100 + 480, 50 + 810
    assert fake_gui.calls == [
        ("click", px, py, 1, "left"),
        ("click", px, py, 1, "right"),
        ("click", px, py, 2, "left"),
    ]


def test_type_ascii_uses_typewrite(fake_gui: _FakePyAutoGUI) -> None:
    result = computer.type_text(ComputerTypeArgs(text="hello"))
    assert result.ok is True
    assert fake_gui.calls == [("typewrite", "hello")]


def test_type_non_ascii_pastes_via_clipboard(
    fake_gui: _FakePyAutoGUI, monkeypatch: Any
) -> None:
    pasted: list[str] = []
    monkeypatch.setitem(
        sys.modules, "pyperclip", type("P", (), {"copy": staticmethod(pasted.append)})
    )
    result = computer.type_text(ComputerTypeArgs(text="héllo — 日本語"))
    assert result.ok is True
    assert "clipboard" in result.message
    assert pasted == ["héllo — 日本語"]
    assert fake_gui.calls == [("hotkey", "ctrl", "v")]


def test_key_combo_and_single_key(fake_gui: _FakePyAutoGUI) -> None:
    assert computer.key_press(ComputerKeyArgs(keys="ctrl+s")).ok is True
    assert computer.key_press(ComputerKeyArgs(keys="enter")).ok is True
    assert computer.key_press(ComputerKeyArgs(keys="cmd+tab")).ok is True
    assert fake_gui.calls == [
        ("hotkey", "ctrl", "s"),
        ("press", "enter"),
        ("hotkey", "win", "tab"),
    ]


def test_key_unknown_name_rejected(fake_gui: _FakePyAutoGUI) -> None:
    result = computer.key_press(ComputerKeyArgs(keys="ctrl+bogus"))
    assert result.ok is False
    assert "bogus" in result.message
    assert fake_gui.calls == []


def test_scroll_directions(fake_gui: _FakePyAutoGUI) -> None:
    scroll_args = ComputerScrollArgs.model_validate
    assert computer.scroll(scroll_args({"direction": "up", "amount": 5})).ok is True
    assert computer.scroll(scroll_args({"direction": "down"})).ok is True
    assert computer.scroll(scroll_args({"direction": "left"})).ok is True
    assert computer.scroll(
        scroll_args({"direction": "right", "x": 500, "y": 500})
    ).ok is True
    assert fake_gui.calls == [
        ("scroll", 5),
        ("scroll", -3),
        ("hscroll", -3),
        ("moveTo", 1060, 590),
        ("hscroll", 3),
    ]


def test_scroll_invalid_direction(fake_gui: _FakePyAutoGUI) -> None:
    result = computer.scroll(ComputerScrollArgs.model_validate({"direction": "sideways"}))
    assert result.ok is False
    assert fake_gui.calls == []


def test_failsafe_returns_error(monkeypatch: Any) -> None:
    fake = _FakePyAutoGUI(failsafe=True)
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    monkeypatch.setattr(
        "media.screen.monitor_geometry", lambda monitor=1: FAKE_GEOMETRY
    )
    result = computer.left_click(ComputerClickArgs(x=500, y=500))
    assert result.ok is False
    assert "failsafe" in result.message.lower()


# -- Policy gating --


@pytest.fixture
def policy(tmp_path: Path) -> PolicyEngine:
    return PolicyEngine(
        allow_roots=[tmp_path], deny_globs=[], never_allow_regex=[]
    )


@pytest.mark.parametrize(
    "name",
    [
        "computer_left_click",
        "computer_right_click",
        "computer_double_click",
        "computer_type",
        "computer_key",
        "computer_drag",
        "computer_scroll",
    ],
)
def test_state_changing_actions_require_confirmation(
    policy: PolicyEngine, name: str
) -> None:
    schema = {spec.name: spec.args_schema for spec in COMPUTER_TOOLS}[name]
    args = _sample_args(schema)
    assert policy.evaluate(name, args).kind == DecisionKind.CONFIRM


def test_yolo_mode_allows_state_changing_actions(policy: PolicyEngine) -> None:
    policy.set_gating_mode(GatingMode.YOLO)
    for name in [
        "computer_left_click",
        "computer_right_click",
        "computer_double_click",
        "computer_type",
        "computer_key",
        "computer_drag",
        "computer_scroll",
    ]:
        schema = {spec.name: spec.args_schema for spec in COMPUTER_TOOLS}[name]
        args = _sample_args(schema)
        assert policy.evaluate(name, args).kind == DecisionKind.ALLOW, name


@pytest.mark.parametrize("name", ["computer_mouse_move", "computer_screenshot"])
def test_readonly_actions_are_allowed(policy: PolicyEngine, name: str) -> None:
    schema = {spec.name: spec.args_schema for spec in COMPUTER_TOOLS}[name]
    assert policy.evaluate(name, _sample_args(schema)).kind == DecisionKind.ALLOW


def _sample_args(schema: Any) -> Any:
    """Build minimal valid args for a computer tool's pydantic model."""
    values: dict[str, Any] = {}
    for field_name, field in schema.model_fields.items():
        if not field.is_required():
            continue
        annotation = field.annotation
        values[field_name] = 500 if annotation is int else "enter"
    return schema.model_validate(values)


# -- Registry declarations --


def _declared_names(tools: Any) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        for decl in tool.function_declarations or []:
            if decl.name:
                names.add(decl.name)
    return names


def test_computer_tools_excluded_by_default() -> None:
    names = _declared_names(build_function_declarations())
    assert names == {spec.name for spec in ALL_TOOLS}
    assert not names & COMPUTER_TOOL_NAMES


def test_computer_tools_included_when_enabled() -> None:
    names = _declared_names(build_function_declarations(computer_use=True))
    assert names == {spec.name for spec in ALL_TOOLS} | COMPUTER_TOOL_NAMES
    assert len(COMPUTER_TOOL_NAMES) == len(COMPUTER_TOOLS) == 9


# -- Settings & system prompt --


def _make_settings(workspace: Path, **overrides: Any) -> AppSettings:
    base: dict[str, Any] = {
        "model": "gemini-3.1-flash-live-preview",
        "mode": "text",
        "working_dir": workspace,
        "workspace_root": workspace,
        "wake_word_enabled": False,
        "computer_use_enabled": False,
        "yolo": False,
        "share_screen": False,
        "minimize_to_tray": False,
    }
    base.update(overrides)
    return AppSettings(**base)


def _instruction_text(settings: AppSettings) -> str:
    config = settings.as_connect_config(tools=[])
    instruction = config.system_instruction
    assert isinstance(instruction, types.Content)
    assert instruction.parts is not None
    return "".join(
        part.text or "" for part in instruction.parts if isinstance(part, types.Part)
    )


def test_computer_use_prompt_section_only_when_enabled(tmp_path: Path) -> None:
    off = _instruction_text(_make_settings(tmp_path))
    assert "Computer Control" not in off

    on = _instruction_text(_make_settings(tmp_path, computer_use_enabled=True))
    assert "## Computer Control (mouse & keyboard)" in on
    assert "computer_left_click" in on
    assert "0-1000" in on


def test_computer_use_implies_screen_note(tmp_path: Path) -> None:
    text = _instruction_text(
        _make_settings(tmp_path, share_screen=False, computer_use_enabled=True)
    )
    assert "Screen Sharing: ACTIVE" in text


def test_computer_use_round_trips_through_settings_file(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "settings.settings.user_config_dir",
        lambda appname, appauthor=None, **kwargs: str(tmp_path),
    )
    _make_settings(tmp_path, computer_use_enabled=True).save()
    loaded = AppSettings.load(working_dir=tmp_path, workspace_root=tmp_path)
    assert loaded.computer_use_enabled is True


def test_gui_checkbox_round_trips(qtbot: Any, tmp_path: Path) -> None:
    window = MainWindow(_make_settings(tmp_path, computer_use_enabled=True))
    qtbot.addWidget(window)
    assert window._computer_use_check.isChecked() is True

    window._computer_use_check.setChecked(False)
    assert window._build_settings().computer_use_enabled is False


# -- Dispatcher round-trip --


@pytest.fixture
def event_queue() -> asyncio.Queue[Any]:
    return asyncio.Queue()


@pytest.fixture
def journal(tmp_path: Path) -> Generator[Journal, None, None]:
    journal = Journal(run_id="test-run", log_dir=tmp_path)
    yield journal
    journal._close()


@pytest.fixture
def dispatcher(
    policy: PolicyEngine,
    event_queue: asyncio.Queue[Any],
    journal: Journal,
    tmp_path: Path,
) -> ToolDispatcher:
    set_policy_engine(policy)
    set_workspace_root(tmp_path)
    return ToolDispatcher(
        policy=policy,
        journal=journal,
        event_queue=event_queue,
        run_id="test-run",
    )


def _capture_callback(
    responses: list[tuple[str, str, ToolResult]],
) -> Callable[[str, str, ToolResult], Awaitable[bool]]:
    async def respond(call_id: str, name: str, result: ToolResult) -> bool:
        responses.append((call_id, name, result))
        return True

    return respond


async def _drain(queue: asyncio.Queue[Any]) -> list[Any]:
    items: list[Any] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


@pytest.mark.asyncio
async def test_dispatcher_click_yolo_no_approval(
    dispatcher: ToolDispatcher,
    event_queue: asyncio.Queue[Any],
    fake_gui: _FakePyAutoGUI,
) -> None:
    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))
    dispatcher.set_gating_mode(GatingMode.YOLO)

    await dispatcher.submit("call-y1", "computer_left_click", {"x": 250, "y": 250})
    events = await _drain(event_queue)

    assert not any(isinstance(e, ToolApprovalRequested) for e in events)
    result_event = [e for e in events if isinstance(e, ToolResultSent)][0]
    assert result_event.ok is True
    assert fake_gui.calls == [("click", 580, 320, 1, "left")]
    assert responses[0][2].ok is True


@pytest.mark.asyncio
async def test_dispatcher_click_approval_flow(
    dispatcher: ToolDispatcher,
    event_queue: asyncio.Queue[Any],
    fake_gui: _FakePyAutoGUI,
) -> None:
    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))

    task = asyncio.create_task(
        dispatcher.submit("call-c1", "computer_left_click", {"x": 500, "y": 500})
    )
    await asyncio.sleep(0)
    events = await _drain(event_queue)
    assert any(
        isinstance(e, ToolApprovalRequested) and e.call_id == "call-c1"
        for e in events
    )
    assert fake_gui.calls == []  # nothing executed before approval

    dispatcher.approve("call-c1")
    await task

    events = await _drain(event_queue)
    result_event = [e for e in events if isinstance(e, ToolResultSent)][0]
    assert result_event.ok is True
    assert fake_gui.calls == [("click", 1060, 590, 1, "left")]
    assert responses[0][2].ok is True


@pytest.mark.asyncio
async def test_dispatcher_mouse_move_needs_no_approval(
    dispatcher: ToolDispatcher,
    event_queue: asyncio.Queue[Any],
    fake_gui: _FakePyAutoGUI,
) -> None:
    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))

    await dispatcher.submit("call-c2", "computer_mouse_move", {"x": 0, "y": 0})
    events = await _drain(event_queue)

    assert not any(isinstance(e, ToolApprovalRequested) for e in events)
    result_event = [e for e in events if isinstance(e, ToolResultSent)][0]
    assert result_event.ok is True
    assert fake_gui.calls == [("moveTo", 100, 50)]


@pytest.mark.asyncio
async def test_dispatcher_rejects_out_of_range_coordinates(
    dispatcher: ToolDispatcher,
    event_queue: asyncio.Queue[Any],
    fake_gui: _FakePyAutoGUI,
) -> None:
    responses: list[tuple[str, str, ToolResult]] = []
    dispatcher.set_respond_callback(_capture_callback(responses))

    await dispatcher.submit("call-c3", "computer_left_click", {"x": 1500, "y": 0})
    events = await _drain(event_queue)

    result_event = [e for e in events if isinstance(e, ToolResultSent)][0]
    assert result_event.ok is False
    assert "Invalid arguments" in result_event.result
    assert fake_gui.calls == []
