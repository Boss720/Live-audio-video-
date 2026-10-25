"""Tests for main.py entry-point helpers (frozen-app workspace handling)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from main import _guard_frozen_workspace, _resolve_working_dir
from settings.settings import AppSettings


def test_resolve_working_dir_passes_through_explicit_path(tmp_path: Path) -> None:
    assert _resolve_working_dir(str(tmp_path)) == tmp_path


def test_resolve_working_dir_defaults_to_cwd_when_not_frozen(
    monkeypatch: Any,
) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert _resolve_working_dir(".") == Path(".")


def test_resolve_working_dir_defaults_to_documents_when_frozen(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    resolved = _resolve_working_dir(".")

    assert resolved == tmp_path / "Documents" / "GeminiLiveAgent"
    assert resolved.is_dir()  # created on demand


def test_guard_frozen_workspace_redirects_install_dir_workspace(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A persisted Program Files workspace must be replaced when frozen."""
    install_dir = tmp_path / "Program Files" / "GeminiLiveAgent"
    install_dir.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install_dir / "gemini-live-agent.exe"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    settings = AppSettings(
        working_dir=install_dir,
        workspace_root=install_dir,
    )
    guarded = _guard_frozen_workspace(settings)

    assert guarded.working_dir == tmp_path / "Documents" / "GeminiLiveAgent"
    assert guarded.workspace_root == tmp_path / "Documents" / "GeminiLiveAgent"


def test_guard_frozen_workspace_keeps_user_workspace(
    monkeypatch: Any, tmp_path: Path
) -> None:
    install_dir = tmp_path / "Program Files" / "GeminiLiveAgent"
    install_dir.mkdir(parents=True)
    user_ws = tmp_path / "somewhere-else"
    user_ws.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install_dir / "gemini-live-agent.exe"))

    settings = AppSettings(working_dir=user_ws, workspace_root=user_ws)
    guarded = _guard_frozen_workspace(settings)

    assert guarded.working_dir == user_ws


def test_guard_is_a_noop_when_not_frozen(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    settings = AppSettings(working_dir=tmp_path, workspace_root=tmp_path)
    assert _guard_frozen_workspace(settings) is settings


def _capture_startup(monkeypatch: Any, argv: list[str]) -> AppSettings:
    import main

    captured: list[AppSettings] = []
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    monkeypatch.setattr(main, "migrate_dotenv", lambda: False)
    monkeypatch.setattr(main, "get_api_key", lambda: "fake-startup-key")
    monkeypatch.setattr(main, "get_free_api_key", lambda: None)
    monkeypatch.setattr(main, "_with_tier_keys", lambda settings: settings)
    monkeypatch.setattr("app.main_window.run_gui", lambda settings: captured.append(settings) or 0)
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)
    assert main.main() == 0
    return captured[0]


def test_startup_preserves_saved_preferences_without_explicit_flags(
    monkeypatch: Any, tmp_path: Path
) -> None:
    saved = AppSettings(
        _env_file=None, model="gemini-3.8-live", voice="Kore", mode="text",
        working_dir=tmp_path, workspace_root=tmp_path, screen_fps=2.0, screen_monitor=2,
        yolo=False, share_screen=False,
    )
    saved.save()
    loaded = _capture_startup(monkeypatch, [])
    for name in ("model", "voice", "mode", "working_dir", "workspace_root", "screen_fps",
                 "screen_monitor", "yolo", "share_screen"):
        assert getattr(loaded, name) == getattr(saved, name), name


def test_explicit_flags_override_saved_preferences(monkeypatch: Any, tmp_path: Path) -> None:
    AppSettings(
        _env_file=None, model="gemini-3.8-live", voice="Kore", mode="text",
        working_dir=tmp_path, workspace_root=tmp_path,
    ).save()
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    loaded = _capture_startup(monkeypatch, [
        "--model", "custom-live", "--voice", "Puck", "--mode", "both",
        "--working-dir", str(explicit), "--screen-fps", "3", "--monitor", "1",
    ])
    assert (loaded.model, loaded.voice, loaded.mode) == ("custom-live", "Puck", "both")
    assert loaded.working_dir == loaded.workspace_root == explicit
    assert loaded.screen_fps == 3.0
    assert loaded.screen_monitor == 1


def test_first_start_keeps_guarded_share_off_defaults(monkeypatch: Any) -> None:
    loaded = _capture_startup(monkeypatch, [])
    assert not loaded.yolo
    assert not loaded.share_screen
    assert not loaded.headless


def test_headless_without_console_points_to_cli_exe(monkeypatch: Any) -> None:
    """The windowed installed exe has no stdin: --headless must not start."""
    import main

    shown: list[str] = []
    monkeypatch.setattr(sys, "argv", ["main.py", "--headless"])
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(main, "_show_error", shown.append)
    monkeypatch.setattr(main, "run_headless", lambda settings: pytest.fail("ran headless"))
    monkeypatch.setattr(main, "_prompt_for_api_key", lambda: pytest.fail("prompted"))
    assert main.main() == 2
    assert main.CLI_EXE_NAME in shown[0]


def test_show_error_uses_stderr_when_present(capsys: pytest.CaptureFixture[str]) -> None:
    from main import _show_error

    _show_error("boom")
    assert capsys.readouterr().err == "boom\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_show_error_uses_message_box_without_console(monkeypatch: Any) -> None:
    import ctypes

    from main import _show_error

    boxes: list[tuple[object, str, str, int]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(
        ctypes.windll.user32, "MessageBoxW", lambda *a: boxes.append(a) or 1
    )
    _show_error("boom")
    assert boxes == [(None, "boom", "Gemini Live Agent", 0x10)]


def test_negative_flags_override_saved_approval_and_share_preferences(
    monkeypatch: Any, tmp_path: Path
) -> None:
    AppSettings(_env_file=None, working_dir=tmp_path, workspace_root=tmp_path,
                yolo=True, share_screen=True).save()
    loaded = _capture_startup(monkeypatch, ["--no-yolo", "--no-share-screen"])
    assert not loaded.yolo
    assert not loaded.share_screen
