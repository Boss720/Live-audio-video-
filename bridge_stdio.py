"""NDJSON stdio driver for embedding the agent in another process.

Speaks one JSON object per line in both directions:

* **stdin** carries commands (``connect``, ``send_text``, ``approve_tool``, ...)
  which are translated into ``core.commands`` objects and pushed onto the
  supervisor's command queue.
* **stdout** carries events — the ``core.events`` dataclasses, serialized with a
  snake_case ``type`` tag — plus the two bridge-only events ``ready`` and
  ``fatal``.

stdout is reserved for the protocol: every log line goes to stderr (see
``obs.logging_setup``), and nothing else in this module prints.

The process starts idle, emits ``ready``, and only builds a ``Supervisor`` when
a ``connect`` command arrives. When that session ends the process exits, so the
host spawns one child per conversation.

Run it with ``python bridge_stdio.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import re
import sys
import threading
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from audit.journal import Journal
from core.commands import (
    AgentCommand,
    ApproveTool,
    CancelTool,
    DenyTool,
    Disconnect,
    GatingMode,
    SendText,
    SetGatingMode,
    SetMicGate,
    SetShareScreen,
)
from core.supervisor import Supervisor
from obs.logging_setup import get_logger, get_run_id, setup_logging
from persist.store import Store
from settings.secrets import get_api_key, get_free_api_key, migrate_dotenv
from settings.settings import AppSettings

PROTOCOL_VERSION = 1

logger = get_logger(__name__)

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _event_type(event: object) -> str:
    """``PartialTranscript`` -> ``partial_transcript``."""
    return _CAMEL_BOUNDARY.sub("_", type(event).__name__).lower()


def _jsonable(value: Any) -> Any:
    """Coerce a dataclass field into something ``json.dumps`` accepts."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def encode_event(event: Any) -> dict[str, Any]:
    """Serialize a core event dataclass into a protocol frame."""
    payload = {
        key: _jsonable(value) for key, value in dataclasses.asdict(event).items()
    }
    payload["type"] = _event_type(event)
    return payload


class _Writer:
    """Line-buffered NDJSON sink guarded against interleaved writes."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, frame: dict[str, Any]) -> None:
        line = json.dumps(frame, ensure_ascii=False, default=str)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


_EOF = object()


def _parse_bool(val: Any, default: bool = True) -> bool:
    """Parse a boolean value, correctly handling string forms like 'false' or '0'."""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        normalized = val.strip().lower()
        if normalized in {"false", "0", "no", "off"}:
            return False
        if normalized in {"true", "1", "yes", "on"}:
            return True
    return bool(val)


def _start_stdin_thread(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[Any],
    stop_event: threading.Event,
) -> threading.Thread:
    """Push stdin lines onto ``queue`` from a daemon thread, thread-safely."""

    def _reader() -> None:
        try:
            while not stop_event.is_set():
                try:
                    line = sys.stdin.readline()
                except (OSError, ValueError):
                    break
                if not line:
                    break
                loop.call_soon_threadsafe(queue.put_nowait, line.strip())
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _EOF)

    thread = threading.Thread(target=_reader, daemon=True, name="bridge-stdin")
    thread.start()
    return thread


def build_settings(overrides: dict[str, Any]) -> AppSettings:
    """Build ``AppSettings`` from a ``connect`` payload.

    Unknown keys are dropped rather than raising: the host may be a newer or
    older build than this driver, and an unrecognized preference must not stop
    a session from starting. Both tier keys are resolved from the environment
    or credential store when the payload does not carry them, which is what
    arms the dual-key fallback cascade in ``Supervisor``.
    """
    # The host may have no key of its own (its provider store and this
    # harness's are separate), so fall back to the checkout's own .env exactly
    # as `main.py` does before reading the credential store.
    migrate_dotenv()
    known = {
        key: value
        for key, value in overrides.items()
        if key in AppSettings.model_fields
    }
    # The host drives the UI; nothing here may open a Qt window or read stdin.
    known["headless"] = True
    settings = AppSettings.load(**known)

    updates: dict[str, str] = {}
    if not settings.api_key_tier1:
        tier1 = get_api_key()
        if tier1:
            updates["api_key_tier1"] = tier1
    if not settings.api_key_free:
        free = get_free_api_key()
        if free:
            updates["api_key_free"] = free
    return settings.model_copy(update=updates) if updates else settings


def parse_command(frame: dict[str, Any]) -> AgentCommand | None:
    """Translate a protocol frame into a core command.

    Returns ``None`` for frames the supervisor does not consume (``connect``
    and ``ping`` are handled by the driver itself). Raises ``ValueError`` on an
    unknown type or a missing required field, which the caller reports as a
    non-fatal ``session_error``.
    """
    kind = frame.get("type")
    if kind in {"connect", "ping"}:
        return None
    if kind == "disconnect":
        return Disconnect()
    if kind == "send_text":
        text = frame.get("text")
        if not isinstance(text, str) or not text.strip():
            msg = "send_text requires a non-empty 'text'"
            raise ValueError(msg)
        return SendText(text=text)
    if kind == "set_mic_gate":
        return SetMicGate(open=_parse_bool(frame.get("open"), default=True))
    if kind == "set_share_screen":
        monitor = frame.get("monitor")
        monitor_val = (
            int(monitor)
            if isinstance(monitor, int | float | str) and not isinstance(monitor, bool)
            else None
        )
        return SetShareScreen(
            enabled=_parse_bool(frame.get("enabled"), default=True),
            monitor=monitor_val,
        )
    if kind == "approve_tool":
        return ApproveTool(call_id=_require_call_id(frame))
    if kind == "deny_tool":
        reason = frame.get("reason")
        return DenyTool(
            call_id=_require_call_id(frame),
            reason=reason if isinstance(reason, str) and reason else "user_denied",
        )
    if kind == "cancel_tool":
        call_ids = frame.get("call_ids")
        if not isinstance(call_ids, list) or not all(
            isinstance(item, str) for item in call_ids
        ):
            msg = "cancel_tool requires 'call_ids' as a list of strings"
            raise ValueError(msg)
        return CancelTool(call_ids=list(call_ids))
    if kind == "set_gating_mode":
        raw = frame.get("mode")
        if not isinstance(raw, str) or raw not in {mode.value for mode in GatingMode}:
            msg = f"set_gating_mode has an unknown mode: {raw!r}"
            raise ValueError(msg)
        duration = frame.get("duration_minutes")
        dur_val = (
            int(duration)
            if isinstance(duration, int | float) and not isinstance(duration, bool)
            else None
        )
        return SetGatingMode(
            mode=GatingMode(raw),
            duration_minutes=dur_val,
        )
    msg = f"unknown command type: {kind!r}"
    raise ValueError(msg)


def _require_call_id(frame: dict[str, Any]) -> str:
    call_id = frame.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        msg = f"{frame.get('type')} requires a non-empty 'call_id'"
        raise ValueError(msg)
    return call_id


async def _pump_events(
    event_queue: asyncio.Queue[Any],
    writer: _Writer,
) -> None:
    """Forward every core event to stdout until cancelled."""
    while True:
        event = await event_queue.get()
        writer.emit(encode_event(event))


def _drain_events(event_queue: asyncio.Queue[Any], writer: _Writer) -> None:
    """Flush events still queued after the session task finished."""
    while not event_queue.empty():
        writer.emit(encode_event(event_queue.get_nowait()))


async def _await_connect(
    stdin_queue: asyncio.Queue[Any],
    writer: _Writer,
) -> dict[str, Any] | None:
    """Read stdin until a ``connect`` frame arrives. ``None`` = stdin closed."""
    while True:
        line = await stdin_queue.get()
        if line is _EOF or line is None:
            return None
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            writer.emit(
                {"type": "session_error", "message": "malformed JSON command", "fatal": False}
            )
            continue
        if not isinstance(frame, dict):
            continue
        if frame.get("type") == "connect":
            settings = frame.get("settings")
            return settings if isinstance(settings, dict) else {}
        if frame.get("type") == "disconnect":
            return None
        if frame.get("type") == "ping":
            # A liveness probe is legal before connect; only real session
            # commands are out of order here.
            continue
        writer.emit(
            {
                "type": "session_error",
                "message": f"command {frame.get('type')!r} arrived before connect",
                "fatal": False,
            }
        )


async def _command_loop(
    stdin_queue: asyncio.Queue[Any],
    command_queue: asyncio.Queue[AgentCommand],
    writer: _Writer,
) -> None:
    """Translate stdin frames into commands until stdin closes or we're cancelled.

    A closed stdin means the host process is gone, so the session is asked to
    disconnect rather than left running headless and unreachable.
    """
    while True:
        line = await stdin_queue.get()
        if line is _EOF or line is None:
            await command_queue.put(Disconnect())
            break
        if not line:
            continue
        try:
            frame = json.loads(line)
            if not isinstance(frame, dict):
                msg = "command frame must be a JSON object"
                raise ValueError(msg)  # noqa: TRY301
            command = parse_command(frame)
        except (json.JSONDecodeError, ValueError) as error:
            writer.emit(
                {"type": "session_error", "message": str(error), "fatal": False}
            )
            continue
        if command is not None:
            await command_queue.put(command)


async def run_bridge() -> int:
    """Run the stdio bridge until the session ends. Returns a process exit code."""
    run_id = get_run_id()
    writer = _Writer(sys.stdout)

    loop = asyncio.get_running_loop()
    stdin_queue: asyncio.Queue[Any] = asyncio.Queue()
    stop_stdin = threading.Event()
    _start_stdin_thread(loop, stdin_queue, stop_stdin)

    writer.emit({"type": "ready", "protocol": PROTOCOL_VERSION, "run_id": run_id})

    try:
        overrides = await _await_connect(stdin_queue, writer)
        if overrides is None:
            return 0

        try:
            settings = build_settings(overrides)
        except Exception as error:  # noqa: BLE001 - reported, not raised, so the host sees why
            writer.emit({"type": "fatal", "message": f"invalid settings: {error}"})
            return 1

        setup_logging(run_id=run_id, debug=settings.debug)

        command_queue: asyncio.Queue[AgentCommand] = asyncio.Queue()
        event_queue: asyncio.Queue[Any] = asyncio.Queue()

        session_task = asyncio.create_task(
            Supervisor(
                settings=settings,
                command_queue=command_queue,
                event_queue=event_queue,
                journal=Journal(run_id=run_id),
                store=Store(),
                run_id=run_id,
            ).run()
        )
        pump_task = asyncio.create_task(_pump_events(event_queue, writer))
        command_task = asyncio.create_task(
            _command_loop(stdin_queue, command_queue, writer)
        )

        try:
            # `wait` rather than `await session_task`: a supervisor crash must be
            # reported as a `fatal` frame, not raised out of the driver.
            await asyncio.wait({session_task})
        finally:
            for task in (pump_task, command_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            _drain_events(event_queue, writer)

        failure = None if session_task.cancelled() else session_task.exception()
        if failure is not None:
            writer.emit({"type": "fatal", "message": str(failure)})
            return 1

        reason = session_task.result()
        writer.emit({"type": "session_ended", "code": reason.code, "detail": reason.detail})
        return 0 if reason.code == "user_request" else 1
    finally:
        stop_stdin.set()


def main() -> int:
    try:
        return asyncio.run(run_bridge())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
