"""Tool argument models and Gemini function declarations.

Schemas are built explicitly from pydantic models rather than parsed from
function docstrings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, get_args, get_origin

from google.genai import types
from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Structured result returned by every tool implementation."""

    ok: bool
    message: str

    def __str__(self) -> str:
        return self.message


class ReadFileArgs(BaseModel):
    """Arguments for ``read_file``."""

    path: str = Field(..., description="Path to the file to read.")
    start_line: int | None = Field(
        None, description="First line to read (1-indexed, inclusive)."
    )
    end_line: int | None = Field(
        None, description="Last line to read (1-indexed, inclusive)."
    )


class WriteFileArgs(BaseModel):
    """Arguments for ``write_file``."""

    path: str = Field(..., description="Path to the file to write.")
    content: str = Field(..., description="Content to write to the file.")


class ListDirectoryArgs(BaseModel):
    """Arguments for ``list_directory``."""

    path: str = Field(
        ".", description="Path to the directory to list. Defaults to the working directory."
    )


class DeleteFileArgs(BaseModel):
    """Arguments for ``delete_file``."""

    path: str = Field(..., description="Path to the file or empty directory to delete.")


class RunCommandArgs(BaseModel):
    """Arguments for ``run_command``."""

    command: str = Field(..., description="The PowerShell command to execute.")
    working_dir: str | None = Field(
        None, description="Working directory for the command. Defaults to the workspace root."
    )
    timeout: int = Field(
        30,
        ge=1,
        description="Maximum time to wait for the command, in seconds (max 300).",
    )


_COORD_X = "X coordinate, normalized 0-1000 on the shared screen (0 = left edge)."
_COORD_Y = "Y coordinate, normalized 0-1000 on the shared screen (0 = top edge)."


class ComputerMouseMoveArgs(BaseModel):
    """Arguments for ``computer_mouse_move``."""

    x: int = Field(..., ge=0, le=1000, description=_COORD_X)
    y: int = Field(..., ge=0, le=1000, description=_COORD_Y)


class ComputerClickArgs(BaseModel):
    """Arguments for the computer click tools."""

    x: int = Field(..., ge=0, le=1000, description=_COORD_X)
    y: int = Field(..., ge=0, le=1000, description=_COORD_Y)


class ComputerTypeArgs(BaseModel):
    """Arguments for ``computer_type``."""

    text: str = Field(..., description="Text to type into the focused field.")


class ComputerKeyArgs(BaseModel):
    """Arguments for ``computer_key``."""

    keys: str = Field(
        ...,
        description=(
            "Key or combination to press, joined with '+'. Examples: 'enter', "
            "'tab', 'ctrl+s', 'alt+tab', 'ctrl+shift+escape'."
        ),
    )


class ComputerDragArgs(BaseModel):
    """Arguments for ``computer_drag``."""

    x1: int = Field(..., ge=0, le=1000, description="Start " + _COORD_X)
    y1: int = Field(..., ge=0, le=1000, description="Start " + _COORD_Y)
    x2: int = Field(..., ge=0, le=1000, description="End " + _COORD_X)
    y2: int = Field(..., ge=0, le=1000, description="End " + _COORD_Y)


class ComputerScrollArgs(BaseModel):
    """Arguments for ``computer_scroll``."""

    direction: str = Field(
        ...,
        description="Scroll direction: up, down, left, or right.",
        json_schema_extra={"enum": ["up", "down", "left", "right"]},
    )
    amount: int = Field(3, ge=1, le=100, description="Wheel clicks to scroll (1-100).")
    x: int | None = Field(
        None, ge=0, le=1000, description=_COORD_X + " Optional; provide with y."
    )
    y: int | None = Field(
        None, ge=0, le=1000, description=_COORD_Y + " Optional; provide with x."
    )

    @model_validator(mode="after")
    def coordinates_are_a_pair(self) -> ComputerScrollArgs:
        """Reject a half-position instead of silently scrolling elsewhere."""
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be provided together, or both omitted")
        return self


class ComputerScreenshotArgs(BaseModel):
    """Arguments for ``computer_screenshot`` (none)."""


def _is_optional(annotation: type) -> bool:
    origin = get_origin(annotation)
    if origin is not None:
        return type(None) in get_args(annotation)
    return False


def _schema_for_annotation(annotation: type) -> types.Schema:
    """Map a small set of Python types to Gemini schema types."""
    if _is_optional(annotation):
        # Strip the ``None`` union for simplicity; the field is not required.
        args = [a for a in get_args(annotation) if a is not type(None)]
        annotation = args[0] if args else str

    if annotation is int:
        return types.Schema(type=types.Type.INTEGER)
    if annotation is bool:
        return types.Schema(type=types.Type.BOOLEAN)
    return types.Schema(type=types.Type.STRING)


def _build_schema(model: type[BaseModel]) -> types.Schema:
    """Convert a pydantic argument model to an explicit ``types.Schema``."""
    properties: dict[str, types.Schema] = {}
    required: list[str] = []
    for name, field_info in model.model_fields.items():
        annotation = field_info.annotation if field_info.annotation is not None else str
        schema = _schema_for_annotation(annotation)
        if field_info.description:
            schema.description = field_info.description
        if field_info.default is not None and not field_info.is_required():
            schema.default = field_info.default
        extra = field_info.json_schema_extra
        if isinstance(extra, dict):
            enum_values = extra.get("enum")
            if isinstance(enum_values, list) and all(
                isinstance(value, str) for value in enum_values
            ):
                schema.enum = [value for value in enum_values if isinstance(value, str)]
        for constraint in field_info.metadata:
            if getattr(constraint, "ge", None) is not None:
                schema.minimum = constraint.ge
            if getattr(constraint, "le", None) is not None:
                schema.maximum = constraint.le
        properties[name] = schema
        if field_info.is_required():
            required.append(name)

    return types.Schema(
        type=types.Type.OBJECT,
        properties=properties,
        required=required,
        property_ordering=list(properties.keys()),
    )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    args_schema: type[BaseModel]
    destructive: bool


ALL_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="read_file",
        description=(
            "Read the contents of a file. Optionally specify start_line and end_line "
            "(1-indexed, inclusive) to read a specific range of lines."
        ),
        args_schema=ReadFileArgs,
        destructive=False,
    ),
    ToolSpec(
        name="write_file",
        description=(
            "Write content to a file. Creates parent directories if they don't exist. "
            "Overwrites existing files."
        ),
        args_schema=WriteFileArgs,
        destructive=True,
    ),
    ToolSpec(
        name="list_directory",
        description="List the contents of a directory, showing file types and sizes.",
        args_schema=ListDirectoryArgs,
        destructive=False,
    ),
    ToolSpec(
        name="delete_file",
        description="Delete a file or an empty directory.",
        args_schema=DeleteFileArgs,
        destructive=True,
    ),
    ToolSpec(
        name="run_command",
        description=(
            "Execute a PowerShell command and return its output (stdout + stderr combined). "
            "Timeout is in seconds (default 30, max 300)."
        ),
        args_schema=RunCommandArgs,
        destructive=True,
    ),
]

# Mouse/keyboard control tools. Only declared when ``computer_use_enabled``
# is set; coordinates are normalized 0-1000 on the shared monitor.
COMPUTER_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="computer_mouse_move",
        description=(
            "Move the mouse cursor to (x, y). Coordinates are normalized 0-1000 "
            "on the shared screen; (0, 0) is the top-left corner."
        ),
        args_schema=ComputerMouseMoveArgs,
        destructive=False,
    ),
    ToolSpec(
        name="computer_left_click",
        description="Left-click at (x, y), normalized 0-1000 on the shared screen.",
        args_schema=ComputerClickArgs,
        destructive=True,
    ),
    ToolSpec(
        name="computer_right_click",
        description="Right-click at (x, y), normalized 0-1000 on the shared screen.",
        args_schema=ComputerClickArgs,
        destructive=True,
    ),
    ToolSpec(
        name="computer_double_click",
        description="Double-click at (x, y), normalized 0-1000 on the shared screen.",
        args_schema=ComputerClickArgs,
        destructive=True,
    ),
    ToolSpec(
        name="computer_type",
        description=(
            "Type text into the currently focused input field. Click the target "
            "field first to focus it."
        ),
        args_schema=ComputerTypeArgs,
        destructive=True,
    ),
    ToolSpec(
        name="computer_key",
        description=(
            "Press a key or key combination such as 'enter', 'tab', 'ctrl+s', "
            "'alt+tab', or 'ctrl+shift+escape'."
        ),
        args_schema=ComputerKeyArgs,
        destructive=True,
    ),
    ToolSpec(
        name="computer_drag",
        description=(
            "Drag with the left mouse button from (x1, y1) to (x2, y2), all "
            "normalized 0-1000 on the shared screen."
        ),
        args_schema=ComputerDragArgs,
        destructive=True,
    ),
    ToolSpec(
        name="computer_scroll",
        description=(
            "Scroll the mouse wheel at (x, y) or the current cursor position. "
            "direction is up/down/left/right; amount is wheel clicks."
        ),
        args_schema=ComputerScrollArgs,
        destructive=True,
    ),
    ToolSpec(
        name="computer_screenshot",
        description=(
            "Capture a fresh screenshot of the shared screen. The frame is sent "
            "to you as video input right after this call."
        ),
        args_schema=ComputerScreenshotArgs,
        destructive=False,
    ),
]

COMPUTER_TOOL_NAMES: frozenset[str] = frozenset(spec.name for spec in COMPUTER_TOOLS)

# Function-call scheduling classification (Module 3). Values are plain strings
# so callers can serialize them; ``schedule_enums`` maps them to the SDK enums
# (``types.Behavior`` on the declaration, ``types.FunctionResponseScheduling``
# on the function response).
#
# - ``non_blocking_when_idle``: read-only tools; the model keeps talking and the
#   result is folded in when the model is idle.
# - ``non_blocking_silent``: fire-and-forget tools whose result should never
#   trigger generation.
# - ``blocking_interrupt``: mutating/destructive tools; the model waits for the
#   result and ongoing generation is interrupted.
FUNCTION_SCHEDULES: dict[str, str] = {
    "read_file": "non_blocking_when_idle",
    "write_file": "blocking_interrupt",
    "list_directory": "non_blocking_when_idle",
    "delete_file": "blocking_interrupt",
    "run_command": "blocking_interrupt",
    "computer_mouse_move": "non_blocking_silent",
    "computer_left_click": "blocking_interrupt",
    "computer_right_click": "blocking_interrupt",
    "computer_double_click": "blocking_interrupt",
    "computer_type": "blocking_interrupt",
    "computer_key": "blocking_interrupt",
    "computer_drag": "blocking_interrupt",
    "computer_scroll": "blocking_interrupt",
    # Blocking on purpose: the screenshot is on the critical path of every
    # computer-use action. As non_blocking_when_idle the model kept talking
    # ("I'll do it") while the frame was deferred until idle — actions stalled.
    "computer_screenshot": "blocking_interrupt",
}

_SCHEDULE_ENUMS: dict[
    str, tuple[types.Behavior, types.FunctionResponseScheduling]
] = {
    "non_blocking_when_idle": (
        types.Behavior.NON_BLOCKING,
        types.FunctionResponseScheduling.WHEN_IDLE,
    ),
    "non_blocking_silent": (
        types.Behavior.NON_BLOCKING,
        types.FunctionResponseScheduling.SILENT,
    ),
    "blocking_interrupt": (
        types.Behavior.BLOCKING,
        types.FunctionResponseScheduling.INTERRUPT,
    ),
}

# Safe default for a tool missing from FUNCTION_SCHEDULES: block on the result.
_DEFAULT_SCHEDULE = "blocking_interrupt"


def tool_schedules() -> dict[str, str]:
    """Return the plain-string scheduling classification for every tool."""
    return dict(FUNCTION_SCHEDULES)


def schedule_enums(
    name: str,
) -> tuple[types.Behavior, types.FunctionResponseScheduling]:
    """Map a tool's schedule classification to the SDK enums.

    Returns ``(behavior, response_scheduling)``: ``behavior`` belongs on the
    ``FunctionDeclaration``, ``response_scheduling`` on the ``FunctionResponse``
    (ignored by the API for BLOCKING calls).
    """
    return _SCHEDULE_ENUMS[FUNCTION_SCHEDULES.get(name, _DEFAULT_SCHEDULE)]


def _declaration_for(spec: ToolSpec) -> types.FunctionDeclaration:
    behavior, _ = schedule_enums(spec.name)
    return types.FunctionDeclaration(
        name=spec.name,
        description=spec.description,
        parameters=_build_schema(spec.args_schema),
        behavior=behavior,
    )


def build_function_declarations(*, computer_use: bool = False) -> Sequence[types.Tool]:
    """Build explicit Gemini tools from function declarations.

    The Live SDK expects ``list[types.Tool]``; all declarations are consolidated
    into a single ``types.Tool`` (the API allows up to 512 per tool) with each
    declaration's ``behavior`` set from ``FUNCTION_SCHEDULES``. ``computer_use``
    adds the mouse/keyboard control tools.
    """
    specs = ALL_TOOLS + COMPUTER_TOOLS if computer_use else ALL_TOOLS
    return [
        types.Tool(function_declarations=[_declaration_for(spec) for spec in specs])
    ]


# The SDK's code-execution tool exists (types.ToolCodeExecution) but there is no
# AppSettings flag for it yet; flip this to True to enable it.
CODE_EXECUTION_ENABLED = False


def build_tools(settings: Any) -> list[types.Tool]:
    """Build the consolidated tool list for a Live session.

    One ``types.Tool`` holding every function declaration, plus a Google Search
    tool when ``settings.google_search_enabled`` and a code-execution tool when
    ``CODE_EXECUTION_ENABLED`` is on.
    """
    tools: list[types.Tool] = list(
        build_function_declarations(
            computer_use=bool(getattr(settings, "computer_use_enabled", False))
        )
    )
    if getattr(settings, "google_search_enabled", False):
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    if CODE_EXECUTION_ENABLED:
        tools.append(types.Tool(code_execution=types.ToolCodeExecution()))
    return tools
