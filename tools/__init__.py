"""Tool implementations and declarations for the Gemini Live Agent.

No PySide6 here.
"""

from __future__ import annotations

from tools._state import set_gating_mode, set_policy, set_workspace
from tools.fs import delete_file, list_directory, read_file, write_file
from tools.registry import (
    ALL_TOOLS,
    CODE_EXECUTION_ENABLED,
    FUNCTION_SCHEDULES,
    DeleteFileArgs,
    ListDirectoryArgs,
    ReadFileArgs,
    RunCommandArgs,
    ToolResult,
    ToolSpec,
    WriteFileArgs,
    build_function_declarations,
    build_tools,
    schedule_enums,
    tool_schedules,
)
from tools.shell import run_command

__all__ = [
    "ALL_TOOLS",
    "CODE_EXECUTION_ENABLED",
    "FUNCTION_SCHEDULES",
    "DeleteFileArgs",
    "ListDirectoryArgs",
    "ReadFileArgs",
    "RunCommandArgs",
    "ToolResult",
    "ToolSpec",
    "WriteFileArgs",
    "build_function_declarations",
    "build_tools",
    "delete_file",
    "list_directory",
    "read_file",
    "run_command",
    "schedule_enums",
    "set_gating_mode",
    "set_policy",
    "set_workspace",
    "tool_schedules",
    "write_file",
]


def _redact_result(result: ToolResult) -> ToolResult:
    """Convenience helper for callers that want redacted tool results."""
    from policy import redact

    return ToolResult(result.ok, redact(result.message))
