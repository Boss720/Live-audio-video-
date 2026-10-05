"""Module 3: session-level connect-config features.

Verifies that ``LiveSession`` builds a ``LiveConnectConfig`` carrying the
Module 3 API features: media resolution mapping, the affective-dialog flag,
thinking config, safety settings deliberately omitted (rejected by the Live API
backend), conditional google_search (via the
registry's ``build_tools``), and per-declaration ``behavior`` stamped from the
registry's ``FUNCTION_SCHEDULES``.

The config is captured from ``tests.fakes.fake_live.FakeLiveSession.config``
after a fake connection, following the conventions of
``tests/test_session_vad_cost.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from audit.journal import Journal
from core.commands import Disconnect
from core.events import ConnectionState, ConnectionStateChanged, TurnComplete
from core.session import (
    LiveSession,
    _apply_function_behavior,
    _build_tools,
    _response_scheduling_for,
    _session_config_fields,
    _should_reset_turn_gate,
)
from persist.store import Store
from settings.settings import AppSettings
from tests.fakes.fake_live import FakeLive, make_fake_client_factory

# The 2.5 native-audio family accepts google_search and affective dialog;
# the default 3.1 model rejects both at connect time (verified live).
SEARCH_CAPABLE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        model="gemini-3.1-flash-live-preview",
        mode="text",
        working_dir=tmp_path,
        workspace_root=tmp_path,
        wake_word_enabled=False,
        computer_use_enabled=False,
        yolo=False,
        share_screen=False,
    )


def _patch_common(monkeypatch: Any, live: FakeLive) -> None:
    monkeypatch.setattr("google.genai.Client", make_fake_client_factory(live))
    monkeypatch.setattr("core.session.get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr("media.devices.list_input_devices", lambda: [])
    monkeypatch.setattr("media.devices.list_output_devices", lambda: [])


async def _wait_live(event_queue: asyncio.Queue[Any]) -> None:
    while True:
        event = await asyncio.wait_for(event_queue.get(), timeout=5)
        if (
            isinstance(event, ConnectionStateChanged)
            and event.state == ConnectionState.LIVE
        ):
            return


async def _connect_config(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> types.LiveConnectConfig:
    """Run a fake session and return the config it connected with."""
    live = FakeLive()
    _patch_common(monkeypatch, live)
    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    event_queue: asyncio.Queue[Any] = asyncio.Queue()
    journal = Journal(run_id="session-config", log_dir=tmp_path)
    session = LiveSession(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=Store(db_path=tmp_path / "state.db"),
        run_id="session-config",
    )
    task = asyncio.create_task(session.run())
    try:
        await _wait_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None
        return fake_session.config
    finally:
        await command_queue.put(Disconnect())
        await asyncio.wait_for(task, timeout=5)
        journal._close()


def _typed_tools(config: types.LiveConnectConfig) -> list[types.Tool]:
    """Narrow ``config.tools`` to real ``Tool`` objects (mypy union)."""
    return [t for t in (config.tools or []) if isinstance(t, types.Tool)]


# ----------------------------------------------------------------------
# 3.7 media resolution
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("low", types.MediaResolution.MEDIA_RESOLUTION_LOW),
        ("high", types.MediaResolution.MEDIA_RESOLUTION_HIGH),
    ],
)
async def test_media_resolution_mapped(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path, setting: str, expected: Any
) -> None:
    settings.media_resolution = setting
    config = await _connect_config(monkeypatch, settings, tmp_path)
    assert config.media_resolution == expected


# ----------------------------------------------------------------------
# 3.8 affective dialog
# ----------------------------------------------------------------------
@pytest.mark.parametrize("enabled", [True, False])
async def test_enable_affective_dialog_flag(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path, enabled: bool
) -> None:
    settings.model = SEARCH_CAPABLE_MODEL
    settings.enable_affective_dialog = enabled
    config = await _connect_config(monkeypatch, settings, tmp_path)
    assert config.enable_affective_dialog is enabled


async def test_affective_dialog_forced_off_on_unsupported_model(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    # Default fixture model is gemini-3.1-flash-live-preview, which rejects
    # enable_affective_dialog at connect time.
    settings.enable_affective_dialog = True
    config = await _connect_config(monkeypatch, settings, tmp_path)
    assert config.enable_affective_dialog is False


# ----------------------------------------------------------------------
# 3.9 thinking level
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("minimal", types.ThinkingLevel.MINIMAL),
        ("low", types.ThinkingLevel.LOW),
        ("medium", types.ThinkingLevel.MEDIUM),
        ("high", types.ThinkingLevel.HIGH),
    ],
)
async def test_thinking_level_mapped(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path, setting: str, expected: Any
) -> None:
    settings.thinking_level = setting
    config = await _connect_config(monkeypatch, settings, tmp_path)
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == expected


def test_gemini_38_standard_omits_thinking_config(settings: AppSettings) -> None:
    settings.model = "gemini-3.8-live"
    assert "thinking_config" not in _session_config_fields(settings)


def test_gemini_38_extended_thinking_uses_configurable_level(
    settings: AppSettings,
) -> None:
    settings.model = "gemini-3.8-live-extended-thinking"
    settings.thinking_level = "low"
    fields = _session_config_fields(settings)
    assert fields["thinking_config"].thinking_level == types.ThinkingLevel.LOW


@pytest.mark.asyncio
async def test_missing_api_key_returns_a_reported_auth_error(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    monkeypatch.setattr("core.session.get_api_key", lambda: None)
    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    event_queue: asyncio.Queue[Any] = asyncio.Queue()
    journal = Journal(run_id="missing-key", log_dir=tmp_path)
    session = LiveSession(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=Store(db_path=tmp_path / "missing-key.db"),
        run_id="missing-key",
    )

    reason = await session.run()
    events: list[Any] = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())

    assert reason.code == "auth_error"
    assert any(
        isinstance(event, ConnectionStateChanged)
        and event.state == ConnectionState.ERROR
        and "No API key configured" in event.detail
        for event in events
    )
    assert events[-1].state == ConnectionState.DISCONNECTED
    journal._close()


def test_extended_thinking_keeps_turn_gate_during_filler() -> None:
    from types import SimpleNamespace

    filler = SimpleNamespace(
        turn_complete=True, interrupted=False, interaction_status="IN_PROGRESS"
    )
    final = SimpleNamespace(
        turn_complete=True, interrupted=False, interaction_status="IDLE"
    )
    model = "gemini-3.8-live-extended-thinking"

    assert not _should_reset_turn_gate(model, filler)
    assert _should_reset_turn_gate(model, final)
    assert _should_reset_turn_gate("gemini-3.8-live", filler)


@pytest.mark.asyncio
async def test_extended_thinking_filler_and_final_are_one_persisted_turn(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    settings.model = "gemini-3.8-live-extended-thinking"
    live = FakeLive()
    _patch_common(monkeypatch, live)
    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    event_queue: asyncio.Queue[Any] = asyncio.Queue()
    journal = Journal(run_id="extended-turn", log_dir=tmp_path)
    store = Store(db_path=tmp_path / "extended-turn.db")
    session = LiveSession(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=store,
        run_id="extended-turn",
    )
    runner = asyncio.create_task(session.run())
    try:
        await _wait_live(event_queue)
        fake_session = live.latest_session
        assert fake_session is not None

        # generation_complete is normalized to turn_complete by LiveSession;
        # interaction_status must still keep this filler inside the open turn.
        fake_session.say(
            "Checking the result. ",
            generation_complete=True,
            interaction_status=types.InteractionStatus.IN_PROGRESS,
        )
        await asyncio.sleep(0.12)
        filler_events: list[Any] = []
        while not event_queue.empty():
            filler_events.append(event_queue.get_nowait())
        assert not any(isinstance(event, TurnComplete) for event in filler_events)
        assert store.get_turns("extended-turn") == []

        fake_session.say(
            "The final answer.",
            turn_complete=True,
            interaction_status=types.InteractionStatus.IDLE,
        )
        while True:
            event = await asyncio.wait_for(event_queue.get(), timeout=5)
            if isinstance(event, TurnComplete):
                break

        assert event.text == "Checking the result. The final answer."
        assert event.turn_id == 0
        persisted = store.get_turns("extended-turn")
        assert len(persisted) == 1
        assert persisted[0][1] == event.text
    finally:
        await command_queue.put(Disconnect())
        await asyncio.wait_for(runner, timeout=5)
        journal._close()


# ----------------------------------------------------------------------
# 3.12 safety settings — NOT sent: the Live API backend rejects the field
# ("Unknown name \"safetySettings\" at 'setup'"), so the config must omit it.
# ----------------------------------------------------------------------
async def test_safety_settings_omitted(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    config = await _connect_config(monkeypatch, settings, tmp_path)
    assert not config.safety_settings


# ----------------------------------------------------------------------
# 3.6 google_search conditional (via registry.build_tools)
# ----------------------------------------------------------------------
async def test_google_search_included_only_when_enabled(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    from tools import registry as tool_registry

    if getattr(tool_registry, "build_tools", None) is None:
        pytest.skip("tools.registry.build_tools not implemented yet")

    settings.model = SEARCH_CAPABLE_MODEL
    settings.google_search_enabled = True
    config = await _connect_config(monkeypatch, settings, tmp_path)
    assert any(tool.google_search is not None for tool in _typed_tools(config))

    settings.google_search_enabled = False
    config = await _connect_config(monkeypatch, settings, tmp_path)
    assert all(tool.google_search is None for tool in _typed_tools(config))


async def test_gemini_38_live_omits_search_and_thinking_config(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    settings.model = "gemini-3.8-live"
    settings.google_search_enabled = True
    config = await _connect_config(monkeypatch, settings, tmp_path)

    assert all(tool.google_search is None for tool in _typed_tools(config))
    assert config.thinking_config is None
    assert not config.enable_affective_dialog


async def test_google_search_dropped_on_unsupported_model(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    # Default fixture model is gemini-3.1-flash-live-preview, which rejects
    # google_search at connect time even when the setting is enabled.
    settings.google_search_enabled = True
    config = await _connect_config(monkeypatch, settings, tmp_path)
    assert all(tool.google_search is None for tool in _typed_tools(config))


# ----------------------------------------------------------------------
# 3.3 function-call scheduling -> FunctionDeclaration.behavior
# ----------------------------------------------------------------------
async def test_function_behavior_matches_schedules(
    monkeypatch: Any, settings: AppSettings, tmp_path: Path
) -> None:
    from tools import registry as tool_registry

    schedules = getattr(tool_registry, "FUNCTION_SCHEDULES", None)
    if not schedules:
        pytest.skip("tools.registry.FUNCTION_SCHEDULES not implemented yet")

    # Computer tools are only declared when computer_use is enabled; the
    # schedules cover them, so turn it on to check every scheduled name.
    settings.computer_use_enabled = True
    config = await _connect_config(monkeypatch, settings, tmp_path)
    behavior_by_name = {
        declaration.name: declaration.behavior
        for tool in _typed_tools(config)
        for declaration in (tool.function_declarations or [])
    }
    expected_behavior = {
        "non_blocking_when_idle": types.Behavior.NON_BLOCKING,
        "non_blocking_silent": types.Behavior.NON_BLOCKING,
        "blocking_interrupt": types.Behavior.BLOCKING,
    }
    for name, schedule in schedules.items():
        assert behavior_by_name.get(name) == expected_behavior[schedule]


# ----------------------------------------------------------------------
# Helper unit tests (independent of the registry rewrite)
# ----------------------------------------------------------------------
def test_apply_function_behavior_stamps_declarations(monkeypatch: Any) -> None:
    from tools import registry as tool_registry

    monkeypatch.setattr(
        tool_registry,
        "FUNCTION_SCHEDULES",
        {
            "read_file": "non_blocking_when_idle",
            "run_command": "non_blocking_silent",
            "write_file": "blocking_interrupt",
        },
        raising=False,
    )
    tools = _build_tools_for_helper_test()
    stamped = _apply_function_behavior(tools)
    behavior_by_name = {
        declaration.name: declaration.behavior
        for tool in stamped
        for declaration in (tool.function_declarations or [])
    }
    assert behavior_by_name["read_file"] == types.Behavior.NON_BLOCKING
    assert behavior_by_name["run_command"] == types.Behavior.NON_BLOCKING
    assert behavior_by_name["write_file"] == types.Behavior.BLOCKING


def test_extended_thinking_makes_all_function_tools_non_blocking() -> None:
    tools = _apply_function_behavior(
        _build_tools_for_helper_test(), "gemini-3.8-live-extended-thinking"
    )
    declarations = [
        declaration
        for tool in tools
        for declaration in (tool.function_declarations or [])
    ]
    assert declarations
    assert all(
        declaration.behavior == types.Behavior.NON_BLOCKING
        for declaration in declarations
    )


def test_response_scheduling_for_non_blocking_only(monkeypatch: Any) -> None:
    from tools import registry as tool_registry

    monkeypatch.setattr(
        tool_registry,
        "FUNCTION_SCHEDULES",
        {
            "read_file": "non_blocking_when_idle",
            "run_command": "non_blocking_silent",
            "write_file": "blocking_interrupt",
        },
        raising=False,
    )
    assert (
        _response_scheduling_for("read_file")
        == types.FunctionResponseScheduling.WHEN_IDLE
    )
    assert (
        _response_scheduling_for("run_command")
        == types.FunctionResponseScheduling.SILENT
    )
    # Blocking tools carry no scheduling hint; the API ignores it there.
    assert _response_scheduling_for("write_file") is None
    assert _response_scheduling_for("unknown_tool") is None
    assert (
        _response_scheduling_for("write_file", "gemini-3.8-live-extended-thinking")
        == types.FunctionResponseScheduling.WHEN_IDLE
    )


def test_build_tools_falls_back_without_registry_builder(monkeypatch: Any) -> None:
    from tools import registry as tool_registry

    monkeypatch.delattr(tool_registry, "build_tools", raising=False)
    tools = _build_tools(_helper_settings())
    assert tools
    assert any(tool.function_declarations for tool in tools)


def _build_tools_for_helper_test() -> list[types.Tool]:
    return _build_tools(_helper_settings())


def _helper_settings() -> AppSettings:
    return AppSettings(
        model="gemini-3.1-flash-live-preview",
        mode="text",
        working_dir=Path.cwd(),
        workspace_root=Path.cwd(),
    )
