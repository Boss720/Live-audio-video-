"""Provider-facing tool schema constraints remain aligned with Pydantic models."""

from __future__ import annotations

from google.genai import types

from tools.registry import (
    ComputerClickArgs,
    ComputerDragArgs,
    ComputerScrollArgs,
    ReadFileArgs,
    RunCommandArgs,
    _build_schema,
)


def test_click_drag_and_scroll_emit_numeric_coordinate_bounds() -> None:
    click = _build_schema(ComputerClickArgs)
    drag = _build_schema(ComputerDragArgs)
    scroll = _build_schema(ComputerScrollArgs)

    for schema in (click, drag):
        for name in schema.properties or {}:
            prop = schema.properties[name]
            assert prop.minimum == 0
            assert prop.maximum == 1000

    for name in ("x", "y"):
        prop = (scroll.properties or {})[name]
        assert prop.minimum == 0
        assert prop.maximum == 1000
        assert name not in (scroll.required or [])

    amount = (scroll.properties or {})["amount"]
    assert amount.minimum == 1
    assert amount.maximum == 100


def test_scroll_direction_emits_supported_enum() -> None:
    direction = (_build_schema(ComputerScrollArgs).properties or {})["direction"]
    assert direction.type is types.Type.STRING
    assert direction.enum == ["up", "down", "left", "right"]


def test_required_optional_default_and_description_are_preserved() -> None:
    file_schema = _build_schema(ReadFileArgs)
    assert file_schema.required == ["path"]
    assert (file_schema.properties or {})["path"].description == "Path to the file to read."
    assert (file_schema.properties or {})["start_line"].default is None
    assert (
        (file_schema.properties or {})["start_line"].description
        == "First line to read (1-indexed, inclusive)."
    )

    command_schema = _build_schema(RunCommandArgs)
    assert command_schema.required == ["command"]
    assert (command_schema.properties or {})["timeout"].default == 30
    assert (command_schema.properties or {})["timeout"].minimum == 1
