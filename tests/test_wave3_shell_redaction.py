"""Wave 3: shell redaction/truncation/cancellation and redaction patterns."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

import tools._state as _state
from policy.redaction import redact
from tools.registry import RunCommandArgs
from tools.shell import HEAD_KEEP_BYTES, TAIL_KEEP_BYTES, _truncate, run_command

# ---------------------------------------------------------------------------
# Redaction patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, secret",
    [
        ("Authorization: Bearer abc.DEF-123_xyz==", "abc.DEF-123_xyz"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
        ("password=hunter2hunter2", "hunter2hunter2"),
        ('{"api_key": "abcdef123456"}', "abcdef123456"),
        ("client_secret: 's3cret value here'", "s3cret value here"),
        ("AUTH_TOKEN=zzzzzzzz9999", "zzzzzzzz9999"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig_nature-12", "eyJhbGci"),
        ("key sk-ant-api03-" + "A" * 30, "sk-ant-api03"),
        ("key sk-proj-" + "B" * 30, "sk-proj-"),
        ("key sk-" + "C" * 30, "C" * 30),
        ("AIza" + "d" * 35, "AIzaddd"),
        ("ghp_" + "e" * 36, "ghp_eee"),
        ("gho_" + "f" * 36, "gho_fff"),
        ("ghs_" + "g" * 36, "ghs_ggg"),
        ("github_pat_" + "h" * 45, "github_pat_hhh"),
        ("AKIA" + "I" * 16, "AKIAIII"),
        ("xoxb-1234567890-abcdefghij", "xoxb-1234"),
    ],
)
def test_secret_patterns_redacted(text: str, secret: str) -> None:
    out = redact(text)
    assert secret not in out
    assert "REDACTED" in out


def test_kv_keeps_key() -> None:
    assert redact("password=hunter2hunter2").startswith("password=")
    assert redact("api_key: abcdefgh1234").startswith("api_key:")


def test_pem_private_key_crlf_and_variants() -> None:
    for kind in ("RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY"):
        for eol in ("\n", "\r\n"):
            pem = f"-----BEGIN {kind}-----{eol}MIIEabc{eol}def123{eol}-----END {kind}-----"
            out = redact(f"before{eol}{pem}{eol}after")
            assert "MIIEabc" not in out
            assert "def123" not in out
            assert "before" in out and "after" in out


def test_ordinary_text_not_redacted() -> None:
    for text in (
        "token count: 5",
        "tokens: 12",
        "token: 5",
        "the secret to success is practice",
        "Basic configuration is done",
        "mytokenizer=fast",
        "password policy updated",
    ):
        assert redact(text) == text


def test_no_catastrophic_backtracking() -> None:
    start = time.perf_counter()
    redact("token" * 20000 + " " + "a" * 100000)
    redact("-----BEGIN PRIVATE KEY-----" * 2000)
    redact("eyJ" + "a" * 100000)
    assert time.perf_counter() - start < 5


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_truncate_head_and_tail() -> None:
    text = "H" * HEAD_KEEP_BYTES + "M" * 50000 + "T" * TAIL_KEEP_BYTES
    out = _truncate(text)
    assert out.startswith("H" * HEAD_KEEP_BYTES)
    assert out.endswith("T" * TAIL_KEEP_BYTES)
    assert "[50000 bytes omitted]" in out
    assert "M" not in out


def test_truncate_small_unchanged() -> None:
    assert _truncate("hello") == "hello"


# ---------------------------------------------------------------------------
# run_command (real subprocesses)
# ---------------------------------------------------------------------------

pytestmark_win = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


@pytestmark_win
@pytest.mark.asyncio
async def test_redact_before_truncate(tmp_path: Path) -> None:
    _state.set_workspace(tmp_path)
    # Place a secret straddling the head/tail cut boundary region: emit a long
    # padding so the secret sits exactly across the HEAD cut point.
    # Built inside PowerShell so the command text itself does not trip a
    # policy deny pattern left installed by earlier tests.
    pad = HEAD_KEEP_BYTES - 10
    cmd = (
        f"$s = ('a' * {pad}) + 'gh' + 'p_' + ('Z' * 36) + ' ' + ('b' * 20000); "
        "[Console]::Out.Write($s)"
    )
    res = await run_command(RunCommandArgs(command=cmd, working_dir=str(tmp_path), timeout=60))
    assert "ZZZZ" not in res.message
    assert "ghp_" not in res.message
    assert "bytes omitted" in res.message


def _pid_alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return f'"{pid}"' in out


@pytestmark_win
@pytest.mark.asyncio
async def test_cancel_kills_child_process_tree(tmp_path: Path) -> None:
    _state.set_workspace(tmp_path)
    pidfile = tmp_path / "child.pid"
    inner = "Start-Sleep -Seconds 30"
    cmd = (
        f"$p = Start-Process powershell -ArgumentList '-NoProfile','-Command','{inner}' "
        "-PassThru -NoNewWindow; "
        f"Set-Content -Path '{pidfile}' -Value $p.Id; "
        "Wait-Process -Id $p.Id"
    )
    task = asyncio.create_task(
        run_command(RunCommandArgs(command=cmd, working_dir=str(tmp_path), timeout=120))
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not pidfile.exists():  # noqa: ASYNC110
        await asyncio.sleep(0.2)
    assert pidfile.exists(), "child never started"
    await asyncio.sleep(0.3)
    child_pid = int(pidfile.read_text().strip())
    assert _pid_alive(child_pid)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and _pid_alive(child_pid):  # noqa: ASYNC110
        await asyncio.sleep(0.2)
    assert not _pid_alive(child_pid)
