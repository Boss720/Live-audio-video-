"""Safe first-run defaults and thinking-level normalization."""

from __future__ import annotations

import json
from pathlib import Path

from settings.settings import AppSettings


def _make(tmp_path: Path, **overrides: object) -> AppSettings:
    return AppSettings(working_dir=tmp_path, workspace_root=tmp_path, **overrides)  # type: ignore[arg-type]


def test_fresh_defaults_are_safe(tmp_path: Path) -> None:
    s = _make(tmp_path)
    assert s.yolo is False
    assert s.computer_use_enabled is False
    assert s.model == "gemini-3.8-live"


def test_minimal_maps_to_low_on_extended_thinking(tmp_path: Path) -> None:
    s = _make(tmp_path, model="gemini-3.8-live-extended-thinking", thinking_level="minimal")
    assert s.thinking_level == "low"
    s = _make(tmp_path, model="Gemini 3.8 Live Extended Thinking", thinking_level="minimal")
    assert s.thinking_level == "low"


def test_other_levels_untouched_on_extended_thinking(tmp_path: Path) -> None:
    s = _make(tmp_path, model="gemini-3.8-live-extended-thinking", thinking_level="high")
    assert s.thinking_level == "high"


def test_minimal_kept_on_non_extended_models(tmp_path: Path) -> None:
    for model in ("gemini-3.8-live", "gemini-3.1-flash-live-preview"):
        assert _make(tmp_path, model=model, thinking_level="minimal").thinking_level == "minimal"


def test_saved_yolo_true_wins_over_new_default(
    tmp_path: Path, _isolate_user_config_dir: Path
) -> None:
    (_isolate_user_config_dir / "settings.json").write_text(
        json.dumps({"yolo": True, "computer_use_enabled": True}), encoding="utf-8"
    )
    s = AppSettings.load(working_dir=tmp_path, workspace_root=tmp_path)
    assert s.yolo is True
    assert s.computer_use_enabled is True
