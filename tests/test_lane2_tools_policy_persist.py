"""Tests for Lane 2: Tools, Policy, Security & Lifecycle.

Covers:
- tools/shell.py: Non-zero exit code produces ToolResult(ok=False), tree reaping.
- tools/_state.py: Workspace switching revokes previous workspace root.
- policy/engine.py & tools/fs.py: Symlink resolution prevents bypass of allow-root,
  deny-glob, name patterns, and immutable targets on writes.
- core/dispatcher.py, audit/journal.py, persist/store.py: Redaction of secrets across
  Pydantic errors, args, journal entries, store calls, and bounded recent results ordering.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from audit.journal import Journal
from core.dispatcher import ToolDispatcher
from core.events import ToolResultSent
from persist.store import Store
from policy.engine import PathDeniedError, PolicyEngine
from tools import _state
from tools.fs import write_file
from tools.registry import (
    RunCommandArgs,
    WriteFileArgs,
)
from tools.shell import cancel_shell, run_command


@pytest.mark.asyncio
async def test_shell_nonzero_exit_returns_failure(tmp_path: Path) -> None:
    _state.set_workspace(tmp_path)
    # Command that exits with code 1
    cmd = 'powershell -NoProfile -Command "exit 1"' if sys.platform == "win32" else "exit 1"
    res = await run_command(RunCommandArgs(command=cmd, working_dir=str(tmp_path), timeout=30))
    assert res.ok is False
    assert "[exit code: 1]" in res.message


@pytest.mark.asyncio
async def test_shell_zero_exit_returns_success(tmp_path: Path) -> None:
    _state.set_workspace(tmp_path)
    cmd = (
        'powershell -NoProfile -Command "Write-Output hello; exit 0"'
        if sys.platform == "win32"
        else "echo hello"
    )
    res = await run_command(RunCommandArgs(command=cmd, working_dir=str(tmp_path), timeout=30))
    assert res.ok is True
    assert "[exit code: 0]" in res.message


@pytest.mark.asyncio
async def test_shell_cancel_and_tree_reaping(tmp_path: Path) -> None:
    _state.set_workspace(tmp_path)

    # Launch a command that sleeps
    cmd = (
        'powershell -NoProfile -Command "Start-Sleep -Seconds 10"'
        if sys.platform == "win32"
        else "sleep 10"
    )
    task = asyncio.create_task(
        run_command(
            RunCommandArgs(command=cmd, working_dir=str(tmp_path), timeout=30),
            call_id="test-cancel-call",
        )
    )
    await asyncio.sleep(0.5)

    cancelled = await cancel_shell("test-cancel-call")
    assert cancelled is True

    # Await the run_command task which finishes once proc communicates/exits
    res = await task
    assert res.ok is False


def test_workspace_switch_revokes_previous_root(tmp_path: Path) -> None:
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()

    engine = PolicyEngine(allow_roots=[ws1], deny_globs=[], never_allow_regex=[])
    _state.set_policy(engine)
    _state.set_workspace(ws1)

    assert any(os.path.samefile(r, ws1) for r in _state._policy._allow_roots)

    # Switch workspace to ws2
    _state.set_workspace(ws2)
    assert any(os.path.samefile(r, ws2) for r in _state._policy._allow_roots)
    assert not any(os.path.samefile(r, ws1) for r in _state._policy._allow_roots)

    # Attempting to access ws1 now should be denied
    with pytest.raises(PathDeniedError):
        _state._policy.resolve_path(str(ws1 / "test.txt"), _state._workspace_root, for_write=True)


def test_policy_symlink_write_bypass_prevention(tmp_path: Path) -> None:
    allowed_dir = tmp_path / "allowed"
    forbidden_dir = tmp_path / "forbidden"
    allowed_dir.mkdir()
    forbidden_dir.mkdir()

    engine = PolicyEngine(allow_roots=[allowed_dir], deny_globs=["*.secret"], never_allow_regex=[])
    _state.set_policy(engine)
    _state.set_workspace(allowed_dir)

    target_outside = forbidden_dir / "target.txt"
    target_outside.write_text("outside", encoding="utf-8")

    link_to_outside = allowed_dir / "link_outside.txt"
    try:
        link_to_outside.symlink_to(target_outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted in this environment")

    # Attempting to resolve link_to_outside for write must be denied because target escapes roots
    with pytest.raises(PathDeniedError, match="escapes allowed roots"):
        engine.resolve_path(str(link_to_outside), allowed_dir, for_write=True)

    # Attempting to write through symlink via tools.fs must fail
    res = write_file(WriteFileArgs(path=str(link_to_outside), content="hack"))
    assert res.ok is False
    assert "Policy denied" in res.message or "escapes allowed roots" in res.message


def test_policy_symlink_deny_glob_and_name_prevention(tmp_path: Path) -> None:
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()

    engine = PolicyEngine(allow_roots=[allowed_dir], deny_globs=["*.secret"], never_allow_regex=[])
    _state.set_policy(engine)
    _state.set_workspace(allowed_dir)

    target_secret = allowed_dir / "data.secret"
    target_secret.write_text("secret content", encoding="utf-8")

    link_to_secret = allowed_dir / "innocent.txt"
    try:
        link_to_secret.symlink_to(target_secret)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted in this environment")

    # Resolving for write should detect target matches deny glob
    with pytest.raises(PathDeniedError, match="matches deny glob"):
        engine.resolve_path(str(link_to_secret), allowed_dir, for_write=True)

    # Target matching denied name pattern (.env)
    target_env = allowed_dir / ".env"
    target_env.write_text("KEY=val", encoding="utf-8")

    link_to_env = allowed_dir / "settings.txt"
    try:
        link_to_env.symlink_to(target_env)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted in this environment")

    with pytest.raises(PathDeniedError, match="denied name pattern"):
        engine.resolve_path(str(link_to_env), allowed_dir, for_write=True)


def test_policy_symlink_immutable_target_prevention(tmp_path: Path) -> None:
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()

    engine = PolicyEngine(allow_roots=[allowed_dir], deny_globs=[], never_allow_regex=[])
    _state.set_policy(engine)
    _state.set_workspace(allowed_dir)

    immutable_file = engine._immutable_roots[0] / "cant_touch.txt"
    # Even if target is virtual or exists in immutable root
    link_to_imm = allowed_dir / "link_imm.txt"
    try:
        link_to_imm.symlink_to(immutable_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted in this environment")

    with pytest.raises(PathDeniedError):
        engine.resolve_path(str(link_to_imm), allowed_dir, for_write=True)


def test_policy_symlink_checks_mocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    engine = PolicyEngine(allow_roots=[allowed_dir], deny_globs=["*.secret"], never_allow_regex=[])

    fake_link = allowed_dir / "fake_link.txt"
    outside_target = tmp_path / "outside.txt"
    secret_target = allowed_dir / "target.secret"
    env_target = allowed_dir / ".env"
    imm_target = engine._immutable_roots[0] / "file.txt"

    from pathlib import WindowsPath

    target_override: Path = outside_target

    def fake_is_symlink(self: Path) -> bool:
        return self == fake_link

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if self == fake_link:
            return target_override
        return self

    path_cls = WindowsPath if sys.platform == "win32" else Path
    monkeypatch.setattr(path_cls, "is_symlink", fake_is_symlink)
    monkeypatch.setattr(path_cls, "resolve", fake_resolve)

    # 1. Escapes allow roots
    target_override = outside_target
    with pytest.raises(PathDeniedError, match="symlink target escapes allowed roots"):
        engine.resolve_path(str(fake_link), allowed_dir, for_write=True)

    # 2. Matches deny glob
    target_override = secret_target
    with pytest.raises(PathDeniedError, match="symlink target matches deny glob"):
        engine.resolve_path(str(fake_link), allowed_dir, for_write=True)

    # 3. Matches denied name pattern
    target_override = env_target
    with pytest.raises(PathDeniedError, match="symlink target matches denied name pattern"):
        engine.resolve_path(str(fake_link), allowed_dir, for_write=True)

    # 4. Hits immutable root (even if under allow_roots)
    engine_with_imm = PolicyEngine(
        allow_roots=[allowed_dir, imm_target.parent],
        deny_globs=[],
        never_allow_regex=[],
    )
    target_override = imm_target
    with pytest.raises(PathDeniedError, match="symlink target is immutable"):
        engine_with_imm.resolve_path(str(fake_link), allowed_dir, for_write=True)


@pytest.mark.asyncio
async def test_dispatcher_pydantic_validation_redacts_secrets(tmp_path: Path) -> None:
    engine = PolicyEngine(allow_roots=[tmp_path], deny_globs=[], never_allow_regex=[])
    journal = Journal(run_id="test-redact-run", log_dir=tmp_path)
    q: asyncio.Queue[Any] = asyncio.Queue()
    dispatcher = ToolDispatcher(
        policy=engine,
        journal=journal,
        event_queue=q,
        run_id="test-redact-run",
    )

    secret = "AIzaSyD-1234567890123456789012345678901"
    # Pass invalid argument where start_line expects an int but gets a string with secret
    await dispatcher.submit(
        "call-redact-1", "read_file", {"path": "file.txt", "start_line": secret}
    )

    journal.close()

    events: list[Any] = []
    while not q.empty():
        events.append(q.get_nowait())

    result_event = [e for e in events if isinstance(e, ToolResultSent)][0]
    assert result_event.ok is False
    assert secret not in result_event.result
    assert "[REDACTED: Google API key]" in result_event.result


@pytest.mark.asyncio
async def test_dispatcher_journal_and_store_redact_secrets(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    store = Store(db_path=db_path)
    store.start_run("test-run-sec", "2026-01-01T00:00:00Z", "model", "mode", str(tmp_path))

    engine = PolicyEngine(allow_roots=[tmp_path], deny_globs=[], never_allow_regex=[])
    journal = Journal(run_id="test-run-sec", log_dir=tmp_path)
    q: asyncio.Queue[Any] = asyncio.Queue()
    dispatcher = ToolDispatcher(
        policy=engine,
        journal=journal,
        event_queue=q,
        run_id="test-run-sec",
        store=store,
    )

    secret_key = "sk-123456789012345678901234"
    target_file = tmp_path / "secret.txt"
    task = asyncio.create_task(
        dispatcher.submit(
            "call-sec-2",
            "write_file",
            {"path": str(target_file), "content": f"api_key = {secret_key}"},
        )
    )

    # Approve execution
    await asyncio.sleep(0.05)
    dispatcher.approve("call-sec-2")
    await task

    journal.close()

    # Check journal records
    import anyio

    journal_text = await anyio.Path(journal._path).read_text(encoding="utf-8")
    assert secret_key not in journal_text
    assert "[REDACTED: OpenAI-style key]" in journal_text

    # Check store records
    res = store.get_recent_tool_results("test-run-sec")
    assert len(res) == 1
    # Check raw SQL query in SQLite
    row = store._conn.execute(
        "SELECT args, result FROM tool_calls WHERE id = 'call-sec-2'"
    ).fetchone()
    assert secret_key not in row["args"]
    assert secret_key not in (row["result"] or "")
    assert "[REDACTED: OpenAI-style key]" in row["args"]


def test_store_recent_tool_results_bounded_order(tmp_path: Path) -> None:
    store = Store(db_path=tmp_path / "bounded.db")
    run_id = "run-bounded"
    store.start_run(run_id, "2026-01-01T00:00:00Z", "model", "mode", str(tmp_path))

    # Insert 5 completed tool calls
    for i in range(1, 6):
        store.record_tool_call(
            f"tc-{i}",
            run_id,
            "t1",
            "read_file",
            {"i": i},
            f"result-{i}",
            True,
            False,
            0,
        )

    # Query with limit 3: should return the 3 NEWEST results (tc-3, tc-4, tc-5)
    recent = store.get_recent_tool_results(run_id, limit=3)
    assert len(recent) == 3
    ids = [r[0] for r in recent]
    assert ids == ["tc-3", "tc-4", "tc-5"]
