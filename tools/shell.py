"""Shell command tool implementation.

No PySide6 here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys

import tools._state as _state
from policy import PathDeniedError
from policy.redaction import redact
from tools.registry import RunCommandArgs, ToolResult

HEAD_KEEP_BYTES = 2 * 1024  # first 2 KB of each stream
TAIL_KEEP_BYTES = 6 * 1024  # last 6 KB of each stream (errors live at the tail)
_REAP_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 300

_running_shells: dict[str, asyncio.subprocess.Process] = {}
_running_shells_lock = asyncio.Lock()


def _clamp_timeout(value: int | None) -> int:
    if value is None:
        return 30
    return max(1, min(value, MAX_TIMEOUT_SECONDS))


def _truncate(text: str, head: int = HEAD_KEEP_BYTES, tail: int = TAIL_KEEP_BYTES) -> str:
    """Keep the first ``head`` and last ``tail`` bytes, marking what was omitted."""
    data = text.encode("utf-8")
    if len(data) <= head + tail:
        return text
    omitted = len(data) - head - tail
    head_text = data[:head].decode("utf-8", errors="ignore")
    tail_text = data[-tail:].decode("utf-8", errors="ignore")
    return f"{head_text}\n... [{omitted} bytes omitted] ...\n{tail_text}"


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate a process and all its children without blocking the event loop."""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(proc.pid),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=_REAP_TIMEOUT_SECONDS)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Wait (bounded) for ``proc`` to exit after it has been killed."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_SECONDS)


async def run_command(args: RunCommandArgs, *, call_id: str = "") -> ToolResult:
    """Run a PowerShell command (Windows) or shell command (other platforms)."""
    decision = _state._policy.evaluate("run_command", args)
    if decision.is_denied():
        return ToolResult(False, f"Policy denied: {decision.reason}")

    cwd = _state._workspace_root
    if args.working_dir:
        try:
            cwd = _state._policy.resolve_path(
                args.working_dir, _state._workspace_root, for_write=False
            )
        except PathDeniedError as exc:
            return ToolResult(False, str(exc))
        if not cwd.is_dir():
            return ToolResult(False, f"Error: Not a directory: {cwd}")

    timeout = _clamp_timeout(args.timeout)

    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                args.command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                args.command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                start_new_session=True,
            )

        if call_id:
            async with _running_shells_lock:
                _running_shells[call_id] = proc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.CancelledError:
            await _kill_process_tree(proc)
            await _reap(proc)
            raise
        except TimeoutError:
            await _kill_process_tree(proc)
            await _reap(proc)
            return ToolResult(
                False,
                f"Error: Command timed out after {timeout}s.\nCommand: {args.command}",
            )
        finally:
            if call_id:
                # Plain pop (no await) so cleanup survives a second cancellation.
                _running_shells.pop(call_id, None)

        # Redact the full streams first so a secret cannot be split by truncation.
        stdout = redact(stdout_bytes.decode("utf-8", errors="replace")) if stdout_bytes else ""
        stderr = redact(stderr_bytes.decode("utf-8", errors="replace")) if stderr_bytes else ""

        parts: list[str] = []
        if stdout:
            parts.append(f"[stdout]\n{_truncate(stdout)}")
        if stderr:
            parts.append(f"[stderr]\n{_truncate(stderr)}")

        is_ok = proc.returncode == 0
        exit_info = f"[exit code: {proc.returncode}]"
        if not parts:
            return ToolResult(is_ok, f"Command completed with no output. {exit_info}")

        return ToolResult(is_ok, "\n".join(parts) + f"\n{exit_info}")
    except OSError as exc:
        return ToolResult(False, f"Error executing command: {exc}")


async def cancel_shell(call_id: str) -> bool:
    """Kill a running shell process registered under ``call_id``.

    Returns ``True`` if a process was found and killed. Safe to call multiple
    times or with an unknown id.
    """
    async with _running_shells_lock:
        proc = _running_shells.pop(call_id, None)

    if proc is None:
        return False

    await _kill_process_tree(proc)
    return True
