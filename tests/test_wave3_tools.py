"""Wave 3 tool tests: edit_file, read_file truncation, schema bounds."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.commands import GatingMode
from policy.engine import DecisionKind, PolicyEngine
from tools import _state
from tools.fs import MAX_READ_BYTES, edit_file, read_file
from tools.registry import (
    ALL_TOOLS,
    EditFileArgs,
    ReadFileArgs,
    RunCommandArgs,
    tool_schedules,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    engine = PolicyEngine(allow_roots=[tmp_path], deny_globs=["*.secret"], never_allow_regex=[])
    _state.set_policy(engine)
    _state.set_workspace(tmp_path)
    return tmp_path


def test_edit_file_single_replacement(workspace: Path) -> None:
    target = workspace / "a.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    res = edit_file(EditFileArgs(path=str(target), old_text="beta", new_text="gamma"))
    assert res.ok, res.message
    assert "1 occurrence" in res.message
    assert target.read_text(encoding="utf-8") == "alpha gamma\n"


def test_edit_file_not_found(workspace: Path) -> None:
    target = workspace / "a.txt"
    target.write_text("alpha", encoding="utf-8")
    res = edit_file(EditFileArgs(path=str(target), old_text="zzz", new_text="x"))
    assert not res.ok
    assert "not found" in res.message
    assert target.read_text(encoding="utf-8") == "alpha"


def test_edit_file_ambiguous_requires_replace_all(workspace: Path) -> None:
    target = workspace / "a.txt"
    target.write_text("x x x", encoding="utf-8")
    res = edit_file(EditFileArgs(path=str(target), old_text="x", new_text="y"))
    assert not res.ok
    assert "3 times" in res.message
    assert target.read_text(encoding="utf-8") == "x x x"
    res = edit_file(EditFileArgs(path=str(target), old_text="x", new_text="y", replace_all=True))
    assert res.ok
    assert "3 occurrence" in res.message
    assert target.read_text(encoding="utf-8") == "y y y"


def test_edit_file_preserves_crlf(workspace: Path) -> None:
    target = workspace / "crlf.txt"
    target.write_bytes(b"one\r\ntwo\r\n")
    res = edit_file(EditFileArgs(path=str(target), old_text="two", new_text="2"))
    assert res.ok
    assert target.read_bytes() == b"one\r\n2\r\n"


def test_edit_file_missing_file_and_empty_old_text(workspace: Path) -> None:
    res = edit_file(EditFileArgs(path=str(workspace / "nope.txt"), old_text="a", new_text="b"))
    assert not res.ok
    with pytest.raises(ValidationError):
        EditFileArgs(path="a", old_text="", new_text="b")


def test_edit_file_respects_deny_glob_and_roots(workspace: Path, tmp_path_factory) -> None:
    secret = workspace / "k.secret"
    secret.write_text("abc", encoding="utf-8")
    res = edit_file(EditFileArgs(path=str(secret), old_text="abc", new_text="d"))
    assert not res.ok
    assert secret.read_text(encoding="utf-8") == "abc"

    outside = tmp_path_factory.mktemp("outside") / "o.txt"
    outside.write_text("abc", encoding="utf-8")
    res = edit_file(EditFileArgs(path=str(outside), old_text="abc", new_text="d"))
    assert not res.ok
    assert outside.read_text(encoding="utf-8") == "abc"


def test_edit_file_policy_matches_write_file(workspace: Path) -> None:
    engine = _state._policy
    for mode, expected in ((GatingMode.GUARDED, DecisionKind.CONFIRM),):
        engine.set_gating_mode(mode)
        w = engine.evaluate("write_file", ReadFileArgs(path="a.txt"))
        e = engine.evaluate("edit_file", ReadFileArgs(path="a.txt"))
        assert w.kind == e.kind == expected
    engine.set_gating_mode(GatingMode.YOLO)
    assert engine.evaluate("edit_file", ReadFileArgs(path="a.txt")).kind == DecisionKind.ALLOW


def test_edit_file_registered_and_scheduled() -> None:
    names = [t.name for t in ALL_TOOLS]
    assert "edit_file" in names
    assert tool_schedules()["edit_file"] == "blocking_interrupt"
    spec = next(t for t in ALL_TOOLS if t.name == "edit_file")
    assert spec.destructive


def test_read_file_truncation_names_next_start_line(workspace: Path) -> None:
    target = workspace / "big.txt"
    line = "x" * 99 + "\n"  # 100 bytes
    target.write_text(line * 1500, encoding="utf-8", newline="\n")  # 150,000 bytes
    res = read_file(ReadFileArgs(path=str(target)))
    assert res.ok
    kept_lines = MAX_READ_BYTES // 100  # 655 whole lines fit
    assert f"start_line={kept_lines + 1}" in res.message
    cont = read_file(ReadFileArgs(path=str(target), start_line=kept_lines + 1))
    assert cont.ok
    assert f"[Lines {kept_lines + 1}-1500 of 1500 total]" in cont.message


def test_read_file_under_cap_not_truncated(workspace: Path) -> None:
    target = workspace / "small.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    res = read_file(ReadFileArgs(path=str(target)))
    assert "truncated" not in res.message


def test_schema_bounds() -> None:
    with pytest.raises(ValidationError):
        ReadFileArgs(path="a", start_line=0)
    with pytest.raises(ValidationError):
        ReadFileArgs(path="a", end_line=-1)
    with pytest.raises(ValidationError):
        RunCommandArgs(command="x", timeout=301)
    with pytest.raises(ValidationError):
        RunCommandArgs(command="x", timeout=0)
    assert RunCommandArgs(command="x", timeout=300).timeout == 300
