"""Test-suite-wide isolation from the user's real configuration.

``AppSettings.save()`` writes to ``user_config_dir("GeminiLiveAgent")``, which
is the *live* config of an installed app. Most tests patch that directory, but
a test only has to forget once — a GUI test that saved through the settings
dialog rewrote the real file, leaving the installed app with
``share_screen``/``computer_use_enabled`` off and a working directory pointing
into a deleted pytest tmp dir.

Redirecting the directory for every test makes that class of accident
impossible rather than relying on each test to remember.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_config_dir(tmp_path_factory: Any, monkeypatch: Any) -> Path:
    """Point ``user_config_dir`` at a per-test directory."""
    config_dir = tmp_path_factory.mktemp("user-config")
    monkeypatch.setattr(
        "settings.settings.user_config_dir",
        lambda *args, **kwargs: str(config_dir),
    )
    return config_dir
