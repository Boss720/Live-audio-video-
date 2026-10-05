"""Settings-file compatibility across builds.

A single settings.json is shared by every installed build. A file written by a
newer build must not stop an older one from starting, and the older one must not
destroy the newer one's keys when it saves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from settings.settings import AppSettings, canonicalize_model_name


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: Any) -> Path:
    """Point the settings module at a throwaway config directory."""
    monkeypatch.setattr(
        "settings.settings.user_config_dir",
        lambda *_args, **_kwargs: str(tmp_path),
    )
    return tmp_path


def _write_settings(config_dir: Path, values: dict[str, Any]) -> Path:
    settings_file = config_dir / "settings.json"
    settings_file.write_text(json.dumps(values, indent=2), encoding="utf-8")
    return settings_file


def test_load_ignores_unknown_file_keys(config_dir: Path, tmp_path: Path) -> None:
    """An unmodelled key in settings.json is skipped, not fatal.

    Regression: the packaged 0.2.0 build died at startup with
    "8 validation errors for AppSettings / Extra inputs are not permitted"
    once a newer build had added dual-tier fields to the shared file.
    """
    # Stand-ins for whatever a future build adds; using a real current field
    # would test nothing, since this model already knows it.
    _write_settings(
        config_dir,
        {
            "voice": "Charon",
            "future_reasoning_budget": 4096,
            "future_provider": "somewhere",
            "future_flag": True,
        },
    )

    settings = AppSettings.load(working_dir=tmp_path, workspace_root=tmp_path)

    assert settings.voice == "Charon"
    assert not hasattr(settings, "future_reasoning_budget")


def test_save_preserves_unknown_file_keys(config_dir: Path, tmp_path: Path) -> None:
    """Saving from an older build keeps the newer build's settings intact."""
    settings_file = _write_settings(
        config_dir,
        {"voice": "Charon", "future_flag": "medium", "future_budget": 24000},
    )

    settings = AppSettings.load(working_dir=tmp_path, workspace_root=tmp_path)
    settings.voice = "Puck"
    settings.save()

    written = json.loads(settings_file.read_text(encoding="utf-8"))
    assert written["voice"] == "Puck"
    assert written["future_flag"] == "medium"
    assert written["future_budget"] == 24000


def test_known_keys_still_win_over_preserved_ones(
    config_dir: Path, tmp_path: Path
) -> None:
    """A preserved key never shadows a field the model actually has."""
    settings_file = _write_settings(config_dir, {"voice": "Charon", "unknown_key": 1})

    settings = AppSettings.load(working_dir=tmp_path, workspace_root=tmp_path)
    settings.save()

    written = json.loads(settings_file.read_text(encoding="utf-8"))
    assert written["voice"] == "Charon"
    assert written["unknown_key"] == 1


def test_unknown_override_still_rejected(config_dir: Path, tmp_path: Path) -> None:
    """Strictness is kept where it catches typos: env vars and overrides."""
    with pytest.raises(ValidationError):
        AppSettings.load(
            working_dir=tmp_path,
            workspace_root=tmp_path,
            voise="Charon",  # typo, not a real field
        )


def test_canonicalize_model_name() -> None:
    assert (
        canonicalize_model_name("Gemini 3.8 Live · Extended Thinking")
        == "gemini-3.8-live-extended-thinking"
    )
    assert (
        canonicalize_model_name("Gemini 3.8 Live • Extended Thinking")
        == "gemini-3.8-live-extended-thinking"
    )
    assert (
        canonicalize_model_name("extended thinking")
        == "gemini-3.8-live-extended-thinking"
    )
    assert canonicalize_model_name("Gemini 3.8 Live") == "gemini-3.8-live"
    assert canonicalize_model_name("models/gemini-3.8-live") == "gemini-3.8-live"
    assert (
        canonicalize_model_name("gemini-3.8-live-extended-thinking")
        == "gemini-3.8-live-extended-thinking"
    )
    assert canonicalize_model_name("models/custom-model") == "models/custom-model"
    assert canonicalize_model_name("my-custom-endpoint") == "my-custom-endpoint"


def test_load_canonicalizes_model_and_fallback(
    config_dir: Path, tmp_path: Path
) -> None:
    _write_settings(
        config_dir,
        {
            "model": "Gemini 3.8 Live · Extended Thinking",
            "fallback_model": "Gemini 2.5 Flash Native Audio Preview",
        },
    )
    settings = AppSettings.load(working_dir=tmp_path, workspace_root=tmp_path)
    assert settings.model == "gemini-3.8-live-extended-thinking"
    assert (
        settings.fallback_model
        == "gemini-2.5-flash-native-audio-preview-12-2025"
    )

