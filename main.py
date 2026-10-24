"""Entry point for the Gemini Live Agent.

Usage:
    python main.py                              # GUI (default)
    python main.py --headless                   # Terminal mode (text + voice)
    python main.py --headless --mode text       # Terminal, text-only
    python main.py --mode voice --voice Kore    # GUI, voice-only with Kore
    python main.py --yolo                       # Skip safety confirmations
    python main.py --working-dir C:\\Projects    # Set working directory
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from headless import run_headless
from settings import (
    AVAILABLE_VOICES,
    AppSettings,
    get_api_key,
    get_free_api_key,
    migrate_dotenv,
    set_api_key,
)

DEFAULT_MODEL = "gemini-3.8-live"
DEFAULT_VOICE = "Puck"


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Gemini 3.1 Live — Agentic harness with local computer access",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python main.py                              Start in text + voice mode
  python main.py --mode text                  Text-only (no microphone)
  python main.py --mode voice --voice Kore    Voice-only with the Kore voice
  python main.py --yolo                       Skip all safety confirmations
  python main.py --working-dir C:\\Projects    Set the agent's working directory
  python main.py --share-screen               Let the model see your screen
        """,
    )

    parser.add_argument(
        "--mode",
        choices=["text", "voice", "both"],
        default=None,
        help="Interaction mode (saved setting; first-run default: both)",
    )
    parser.add_argument(
        "--voice",
        choices=AVAILABLE_VOICES,
        default=None,
        help=f"Voice (saved setting; first-run default: {DEFAULT_VOICE})",
    )
    parser.add_argument(
        "--yolo",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Skip confirmation prompts for destructive operations (write, delete, run)",
    )
    parser.add_argument(
        "--working-dir",
        default=None,
        help="Working directory (saved setting; first-run default: current directory)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Model (saved setting; first-run default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--share-screen",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Stream your screen to the model so it can see what you see",
    )
    parser.add_argument(
        "--screen-fps",
        type=float,
        default=None,
        help="Screen frame rate (saved setting; first-run default: 1.0)",
    )
    parser.add_argument(
        "--monitor",
        type=int,
        default=None,
        help="Monitor: 0 for all, 1+ for one screen (saved; first-run default: 1)",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run in headless terminal mode instead of the GUI (default: False)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging",
    )

    return parser.parse_args()


def _prompt_for_api_key() -> None:
    """Prompt the user for an API key and store it in the OS credential store."""
    print(
        "No Google API key found. Please paste your API key and press Enter:",
        file=sys.stderr,
    )
    key = input().strip()
    if not key:
        raise ValueError("API key cannot be empty")
    set_api_key(key)


def _default_workspace() -> Path:
    """User-writable default workspace for the installed app."""
    workspace = Path.home() / "Documents" / "GeminiLiveAgent"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _resolve_working_dir(raw: str) -> Path:
    """Resolve the --working-dir argument into a usable workspace path.

    When running as a frozen (PyInstaller) exe, the process cwd is typically
    the install directory under Program Files — unwritable for standard users
    and marked immutable by the policy engine. Default to a Documents
    workspace in that case.
    """
    if raw != ".":
        return Path(raw)
    if getattr(sys, "frozen", False):
        return _default_workspace()
    return Path(raw)


def _guard_frozen_workspace(settings: AppSettings) -> AppSettings:
    """Redirect a persisted workspace that points at the install directory.

    Older builds persisted working_dir=<Program Files install dir> into
    settings.json on first connect; loading that would re-break file writes.
    """
    if not getattr(sys, "frozen", False):
        return settings
    exe_dir = Path(sys.executable).resolve().parent
    try:
        under_exe = settings.working_dir.resolve().is_relative_to(exe_dir)
    except OSError:  # pragma: no cover - defensive
        under_exe = False
    if not under_exe:
        return settings
    workspace = _default_workspace()
    return settings.model_copy(
        update={"working_dir": workspace, "workspace_root": workspace}
    )


def _with_tier_keys(settings: AppSettings) -> AppSettings:
    """Populate the dual-tier key fields from the environment/credential store.

    The Supervisor builds its TierManager from ``api_key_tier1``/
    ``api_key_free`` (falling back to secrets directly), so resolving both
    keys here wires the fallback cascade for both the GUI and headless entry
    points without changing their Supervisor construction.
    """
    updates: dict[str, str] = {}
    if not settings.api_key_tier1:
        tier1_key = get_api_key()
        if tier1_key:
            updates["api_key_tier1"] = tier1_key
    if not settings.api_key_free:
        free_key = get_free_api_key()
        if free_key:
            updates["api_key_free"] = free_key
    if not updates:
        return settings
    return settings.model_copy(update=updates)


def main() -> int:
    """Parse args, build settings, ensure an API key, and run the agent."""
    args = parse_args()

    migrate_dotenv()
    has_tier1 = get_api_key() is not None
    has_free = get_free_api_key() is not None
    if args.headless and not has_tier1 and not has_free:
        _prompt_for_api_key()

    default_workspace = _resolve_working_dir(".")
    fallbacks: dict[str, Any] = {
        "mode": "both", "voice": DEFAULT_VOICE, "model": DEFAULT_MODEL,
        "yolo": False, "share_screen": False, "screen_fps": 1.0, "screen_monitor": 1,
        "working_dir": default_workspace, "workspace_root": default_workspace,
    }
    overrides = {
        name: getattr(args, name)
        for name in ("mode", "voice", "model", "yolo", "share_screen", "screen_fps")
        if getattr(args, name) is not None
    }
    if args.monitor is not None:
        overrides["screen_monitor"] = args.monitor
    if args.working_dir is not None:
        working_dir = _resolve_working_dir(args.working_dir)
        overrides.update(working_dir=working_dir, workspace_root=working_dir)
    settings = AppSettings.load(
        _fallbacks=fallbacks, headless=args.headless, debug=args.debug, **overrides
    )
    settings = _guard_frozen_workspace(settings)
    settings = _with_tier_keys(settings)

    if settings.headless:
        return asyncio.run(run_headless(settings))

    from app.main_window import run_gui

    return run_gui(settings)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
    except Exception:
        import traceback

        print("\n[ERROR] Fatal error occurred:")
        traceback.print_exc()
        sys.exit(1)
