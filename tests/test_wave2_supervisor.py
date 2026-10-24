"""Wave 2 Supervisor tests: GoAway backoff, ladder reset, replay, usage, free tier."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import core.supervisor as sup_module
from audit.journal import Journal
from core.events import DisconnectReason, UsageUpdate
from core.supervisor import Supervisor
from core.tier_manager import TierManager
from persist.store import Store
from settings.settings import AppSettings


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        mode="text",
        working_dir=tmp_path,
        workspace_root=tmp_path,
        wake_word_enabled=False,
        computer_use_enabled=False,
        yolo=False,
        share_screen=False,
    )


def _make(settings: AppSettings, tmp_path: Path, tier_manager: Any = None) -> Supervisor:
    journal = Journal(run_id="w2", log_dir=tmp_path)
    store = Store(db_path=tmp_path / "state.db")
    return Supervisor(
        settings=settings,
        command_queue=asyncio.Queue(),
        event_queue=asyncio.Queue(),
        journal=journal,
        store=store,
        run_id="w2",
        tier_manager=tier_manager,
    )


class _FakeSession:
    """Scripted LiveSession: each run() pops the next scripted reason."""

    instances: list[_FakeSession] = []
    script: list[Any] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.sent: list[dict[str, Any]] = []
        _FakeSession.instances.append(self)

    async def run(self) -> DisconnectReason:
        item = _FakeSession.script.pop(0)
        if callable(item):
            return item(self)
        return item  # type: ignore[no-any-return]

    async def send_client_content(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


@pytest.fixture
def fake_session(monkeypatch: Any) -> type[_FakeSession]:
    _FakeSession.instances = []
    _FakeSession.script = []
    monkeypatch.setattr(sup_module, "LiveSession", _FakeSession)
    monkeypatch.setattr("media.devices.list_input_devices", lambda: [])
    monkeypatch.setattr("media.devices.list_output_devices", lambda: [])
    return _FakeSession


@pytest.mark.asyncio
async def test_go_away_with_handle_reconnects_instantly(
    settings: AppSettings, tmp_path: Path, fake_session: Any, monkeypatch: Any
) -> None:
    sup = _make(settings, tmp_path)
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr(sup_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sup_module.random, "uniform", lambda a, b: 7.0)

    def go_away(_s: Any) -> DisconnectReason:
        sup._resumption_handle = "h1"
        return DisconnectReason("go_away", "drain")

    fake_session.script = [
        go_away,
        go_away,
        DisconnectReason("user_request", "bye"),
    ]
    reason = await sup.run()
    assert reason.code == "user_request"
    assert sleeps == []
    assert "go_away" not in sup._failure_log or not sup._failure_log["go_away"]


@pytest.mark.asyncio
async def test_go_away_without_handle_still_backs_off(
    settings: AppSettings, tmp_path: Path, fake_session: Any, monkeypatch: Any
) -> None:
    sup = _make(settings, tmp_path)
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr(sup_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sup_module.random, "uniform", lambda a, b: 3.0)
    fake_session.script = [
        DisconnectReason("go_away", "drain"),
        DisconnectReason("user_request", "bye"),
    ]
    await sup.run()
    assert sleeps == [3.0]


@pytest.mark.asyncio
async def test_stable_session_resets_ladder(
    settings: AppSettings, tmp_path: Path, fake_session: Any, monkeypatch: Any
) -> None:
    assert sup_module._STABLE_RESET_SECONDS == 60.0
    sup = _make(settings, tmp_path)
    clock = {"t": 1000.0}
    monkeypatch.setattr(sup_module.time, "monotonic", lambda: clock["t"])
    attempts: list[float] = []
    monkeypatch.setattr(
        sup_module.random, "uniform", lambda a, b: attempts.append(b) or 0.0
    )

    def stable_then_fail(s: Any) -> DisconnectReason:
        sup._connected_at = clock["t"]  # what _on_connected does
        clock["t"] += 61.0
        return DisconnectReason("transient", "x")

    def quick_fail(s: Any) -> DisconnectReason:
        sup._connected_at = clock["t"]
        clock["t"] += 5.0
        return DisconnectReason("transient", "x")

    fake_session.script = [
        quick_fail,
        quick_fail,
        stable_then_fail,
        DisconnectReason("user_request", "bye"),
    ]
    await sup.run()
    # ladder: 0.5, 1.0, then reset after stable run -> back to 0.5
    assert attempts == [0.5, 1.0, 0.5]


@pytest.mark.asyncio
async def test_replay_sends_turn_complete_false(
    settings: AppSettings, tmp_path: Path, fake_session: Any, monkeypatch: Any
) -> None:
    sup = _make(settings, tmp_path)
    monkeypatch.setattr(sup._store, "get_turns", lambda run_id: [("user", "hello", 0)])
    session = _FakeSession()
    await sup._on_connected(session)  # type: ignore[arg-type]
    assert len(session.sent) == 1
    assert session.sent[0]["turn_complete"] is False


@pytest.mark.asyncio
async def test_supervisor_does_not_emit_audio_levels(
    settings: AppSettings, tmp_path: Path, fake_session: Any
) -> None:
    assert not hasattr(Supervisor, "_meter_loop")


def test_usage_recorded_per_event_without_diff(
    settings: AppSettings, tmp_path: Path
) -> None:
    tm = TierManager(tier1_key="k1", free_key=None)
    sup = _make(settings, tmp_path, tm)
    assert sup._current_tier is not None
    calls: list[int] = []
    tm.record_usage = lambda tier, model, n: calls.append(n)  # type: ignore[method-assign]
    sup._record_tier_usage(UsageUpdate(total_tokens=100))
    sup._record_tier_usage(UsageUpdate(total_tokens=80))
    sup._record_tier_usage(UsageUpdate(total_tokens=120))
    assert calls == [100, 80, 120]
    assert not hasattr(sup, "_last_usage_total")


@pytest.mark.asyncio
async def test_free_tier_kwarg_passed(
    settings: AppSettings, tmp_path: Path, fake_session: Any
) -> None:
    free_sup = _make(settings.model_copy(update={"prefer_free_tier": True}), tmp_path,
                     TierManager(tier1_key="k1", free_key="kf"))
    fake_session.script = [DisconnectReason("user_request", "bye")]
    await free_sup.run()
    assert free_sup._current_tier is not None
    assert free_sup._current_tier.tier_name == "free"
    assert fake_session.instances[-1].kwargs["free_tier"] is True

    paid_sup = _make(settings.model_copy(update={"prefer_free_tier": False}), tmp_path,
                     TierManager(tier1_key="k1", free_key="kf"))
    fake_session.script = [DisconnectReason("user_request", "bye")]
    await paid_sup.run()
    assert fake_session.instances[-1].kwargs["free_tier"] is False
