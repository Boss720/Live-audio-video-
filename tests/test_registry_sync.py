"""Test that rendered system instruction and registry stay in sync."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from google.genai import types

from settings.settings import AppSettings
from tools.registry import ALL_TOOLS, COMPUTER_TOOLS


def _make_settings(workspace: Path, **overrides: Any) -> AppSettings:
    """Create AppSettings for testing."""
    base: dict[str, Any] = {
        "model": "gemini-3.1-flash-live-preview",
        "mode": "text",
        "working_dir": workspace,
        "workspace_root": workspace,
        "share_screen": False,
        "minimize_to_tray": False,
    }
    base.update(overrides)
    return AppSettings(**base)


def _instruction_text(settings: AppSettings) -> str:
    """Extract instruction text from LiveConnectConfig."""
    config = settings.as_connect_config(tools=[])
    instruction = config.system_instruction
    assert isinstance(instruction, types.Content)
    assert instruction.parts is not None
    return "".join(
        part.text or "" for part in instruction.parts if isinstance(part, types.Part)
    )


def _extract_tool_names_from_instruction(text: str) -> set[str]:
    """Extract tool names from the rendered instruction.

    Tool names appear in markdown bold in the format "**name**".
    """
    # Match patterns like "**name** —" to extract tool names
    matches = re.findall(r"\*\*([a-z_]+)\*\*", text)
    # Filter to only tool-like names (contain letters/underscores)
    return {name for name in matches if name not in ("e.g", "e.g.")}


def test_all_tools_appear_in_instruction_without_computer_use(
    tmp_path: Path,
) -> None:
    """All tools from registry appear in the rendered instruction."""
    settings = _make_settings(tmp_path, computer_use_enabled=False)
    text = _instruction_text(settings)
    instruction_tools = _extract_tool_names_from_instruction(text)

    registry_tools = {spec.name for spec in ALL_TOOLS}

    # All registry tools should appear
    assert registry_tools.issubset(instruction_tools), (
        f"Missing tools in instruction: {registry_tools - instruction_tools}"
    )


def test_computer_tools_appear_in_instruction_with_computer_use(
    tmp_path: Path,
) -> None:
    """Computer tools appear when computer_use_enabled is True."""
    settings = _make_settings(tmp_path, computer_use_enabled=True, share_screen=True)
    text = _instruction_text(settings)
    instruction_tools = _extract_tool_names_from_instruction(text)

    all_tools = ALL_TOOLS + COMPUTER_TOOLS
    registry_tools = {spec.name for spec in all_tools}

    # All tools should appear
    assert registry_tools.issubset(instruction_tools), (
        f"Missing tools in instruction: {registry_tools - instruction_tools}"
    )


def test_no_removed_tools_appear_in_instruction(tmp_path: Path) -> None:
    """No web_search or other removed tools appear in the instruction."""
    settings = _make_settings(tmp_path)
    text = _instruction_text(settings)

    # web_search was removed from the registry but should not appear in instruction
    assert "web_search" not in text.lower(), (
        "Removed tool 'web_search' still appears in instruction"
    )


def test_instruction_contains_tool_list_section(tmp_path: Path) -> None:
    """The instruction contains a 'Your Tools' section."""
    settings = _make_settings(tmp_path)
    text = _instruction_text(settings)

    assert "## Your Tools" in text, "Instruction missing '## Your Tools' section"
    # Should have numbered entries
    assert "1." in text, "Tool list should have numbered entries"


def test_instruction_and_registry_agree_with_computer_use(
    tmp_path: Path,
) -> None:
    """Rendered instruction tools exactly match the registry when computer_use=True."""
    settings = _make_settings(tmp_path, computer_use_enabled=True, share_screen=True)
    text = _instruction_text(settings)
    instruction_tools = _extract_tool_names_from_instruction(text)

    all_tools = ALL_TOOLS + COMPUTER_TOOLS
    registry_tools = {spec.name for spec in all_tools}

    # Should be a subset match (instruction may have other **bold** text)
    assert registry_tools.issubset(instruction_tools), (
        f"Registry tools not all in instruction: {registry_tools - instruction_tools}"
    )
    # And no extra tool-like names beyond the registry
    extra = instruction_tools - registry_tools
    # Filter out non-tool names like "e.g"
    tool_like_extra = {
        name for name in extra if "_" in name or all(c.isalpha() for c in name)
    }
    # Should be empty or have only guideline/non-tool names
    assert not tool_like_extra, (
        f"Extra tool-like names in instruction: {tool_like_extra}"
    )
