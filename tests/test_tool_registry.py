"""Tests for tool scheduling classification and consolidated tool building."""

from __future__ import annotations

from types import SimpleNamespace

from google.genai import types

from tools.registry import (
    ALL_TOOLS,
    CODE_EXECUTION_ENABLED,
    COMPUTER_TOOLS,
    FUNCTION_SCHEDULES,
    build_function_declarations,
    build_tools,
    schedule_enums,
    tool_schedules,
)

_ALL_SPECS = ALL_TOOLS + COMPUTER_TOOLS
_SCHEDULE_VALUES = {"non_blocking_when_idle", "non_blocking_silent", "blocking_interrupt"}


def _decl_names(tool: types.Tool) -> list[str]:
    return [decl.name or "" for decl in tool.function_declarations or []]


def test_declarations_consolidated_into_single_tool() -> None:
    tools = list(build_function_declarations())
    assert len(tools) == 1
    assert _decl_names(tools[0]) == [spec.name for spec in ALL_TOOLS]


def test_declarations_consolidated_with_computer_use() -> None:
    tools = list(build_function_declarations(computer_use=True))
    assert len(tools) == 1
    assert _decl_names(tools[0]) == [spec.name for spec in _ALL_SPECS]


def test_function_schedules_covers_every_declared_tool() -> None:
    declared = {spec.name for spec in _ALL_SPECS}
    assert set(FUNCTION_SCHEDULES) == declared
    assert set(FUNCTION_SCHEDULES.values()) <= _SCHEDULE_VALUES


def test_declaration_behavior_matches_schedule() -> None:
    (tool,) = build_function_declarations(computer_use=True)
    behaviors = {decl.name: decl.behavior for decl in tool.function_declarations or []}
    assert behaviors["read_file"] is types.Behavior.NON_BLOCKING
    assert behaviors["list_directory"] is types.Behavior.NON_BLOCKING
    assert behaviors["computer_mouse_move"] is types.Behavior.NON_BLOCKING
    assert behaviors["write_file"] is types.Behavior.BLOCKING
    assert behaviors["run_command"] is types.Behavior.BLOCKING
    assert behaviors["computer_left_click"] is types.Behavior.BLOCKING
    # Screenshot is blocking: it is on the critical path of computer use.
    assert behaviors["computer_screenshot"] is types.Behavior.BLOCKING
    # Every declaration has an explicit behavior.
    assert None not in behaviors.values()


def test_tool_schedules_returns_copy_of_mapping() -> None:
    schedules = tool_schedules()
    assert schedules == FUNCTION_SCHEDULES
    assert schedules is not FUNCTION_SCHEDULES


def test_schedule_enums_maps_to_sdk_enums() -> None:
    assert schedule_enums("read_file") == (
        types.Behavior.NON_BLOCKING,
        types.FunctionResponseScheduling.WHEN_IDLE,
    )
    assert schedule_enums("computer_mouse_move") == (
        types.Behavior.NON_BLOCKING,
        types.FunctionResponseScheduling.SILENT,
    )
    assert schedule_enums("write_file") == (
        types.Behavior.BLOCKING,
        types.FunctionResponseScheduling.INTERRUPT,
    )
    # Unknown tools fall back to the safe blocking default.
    assert schedule_enums("not_a_tool")[0] is types.Behavior.BLOCKING


def test_build_tools_google_search_only_when_enabled() -> None:
    enabled = build_tools(SimpleNamespace(google_search_enabled=True))
    disabled = build_tools(SimpleNamespace(google_search_enabled=False))
    assert any(t.google_search is not None for t in enabled)
    assert all(t.google_search is None for t in disabled)


def test_build_tools_includes_computer_tools_when_enabled() -> None:
    tools = build_tools(
        SimpleNamespace(google_search_enabled=False, computer_use_enabled=True)
    )
    (fn_tool,) = [t for t in tools if t.function_declarations]
    assert set(_decl_names(fn_tool)) == {spec.name for spec in _ALL_SPECS}


def test_build_tools_code_execution_absent_by_default() -> None:
    assert CODE_EXECUTION_ENABLED is False
    tools = build_tools(SimpleNamespace(google_search_enabled=True))
    assert all(t.code_execution is None for t in tools)


def test_build_tools_single_function_declarations_tool() -> None:
    tools = build_tools(SimpleNamespace(google_search_enabled=True))
    fn_tools = [t for t in tools if t.function_declarations]
    assert len(fn_tools) == 1
    assert set(_decl_names(fn_tools[0])) == {spec.name for spec in ALL_TOOLS}
