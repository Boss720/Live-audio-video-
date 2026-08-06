"""Module 1 Supervisor fallback-cascade tests.

Exercises the dual-tier rate-limit fallback: on a 429 the Supervisor retries
the same model on the other tier, then the fallback model on each tier, then
backs off (15s -> 30s -> 60s -> 300s ladder, patched to near-zero here) while
emitting countdown events. Also covers resumption-handle propagation across
the cascade and TPM usage recording.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from google.genai import types
from google.genai.errors import ClientError

from audit.journal import Journal
from core.commands import Disconnect
from core.events import (
    ConnectionState,
    ConnectionStateChanged,
    RateLimitHit,
    SessionError,
    UsageUpdate,
)
from core.supervisor import Supervisor
from core.tier_manager import TierManager
from persist.store import Store
from settings import secrets
from settings.settings import AppSettings
from tests.fakes.fake_live import (
    FakeClient,
    FakeLive,
    FakeLiveSession,
)

MODEL = "gemini-3.1-flash-live-preview"
FALLBACK = "gemini-2.5-flash-native-audio-preview-12-2025"
TIER1_KEY = "tier1-key"
FREE_KEY = "free-key"

# Tier 1 TPM limits from TierManager.TIER1_LIMITS, used for enrichment checks.
TIER1_TPM = {MODEL: 150_000, FALLBACK: 1_000_000}


@pytest.fixture(autouse=True)
def _clear_active_key_override() -> Generator[None, None, None]:
    secrets.set_active_api_key(None)
    yield
    secrets.set_active_api_key(None)


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        model=MODEL,
        fallback_model=FALLBACK,
        mode="text",
        working_dir=tmp_path,
        workspace_root=tmp_path,
        wake_word_enabled=False,
        computer_use_enabled=False,
        yolo=False,
        share_screen=False,
    )


@pytest.fixture
def event_queue() -> asyncio.Queue[Any]:
    return asyncio.Queue()


@pytest.fixture
def command_queue() -> asyncio.Queue[Any]:
    return asyncio.Queue()


@pytest.fixture
def journal(tmp_path: Path) -> Generator[Journal, None, None]:
    journal = Journal(run_id="test-run", log_dir=tmp_path)
    yield journal
    journal._close()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(db_path=tmp_path / "state.db")


@pytest.fixture
def tier_manager() -> TierManager:
    return TierManager(tier1_key=TIER1_KEY, free_key=FREE_KEY)


def _quota_error() -> ClientError:
    """A 429 RESOURCE_EXHAUSTED error, as the SDK raises for rate limits."""
    return ClientError(
        429, {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
    )


def _recording_client_factory(live: FakeLive, keys: list[str | None]) -> Any:
    """``genai.Client`` factory that records the api_key of every attempt."""

    def factory(*args: Any, **kwargs: Any) -> FakeClient:
        keys.append(kwargs.get("api_key"))
        return FakeClient(live=live)

    return factory


def _patch_common(monkeypatch: Any, live: FakeLive, keys: list[str | None]) -> None:
    monkeypatch.setattr("google.genai.Client", _recording_client_factory(live, keys))
    monkeypatch.setattr("media.devices.list_input_devices", lambda: [])
    monkeypatch.setattr("media.devices.list_output_devices", lambda: [])
    monkeypatch.setattr("core.supervisor.random.uniform", lambda a, b: 0)
    # Keep backoff delays injectable: near-zero ladder and tick so no test sleeps.
    monkeypatch.setattr("core.supervisor._RATE_LIMIT_BACKOFF_LADDER", (0.05,))
    monkeypatch.setattr("core.supervisor._BACKOFF_TICK_SECONDS", 0.01)


async def _wait_for_event(
    queue: asyncio.Queue[Any],
    buffer: list[Any],
    predicate: Any,
    timeout_seconds: float = 2.0,
) -> Any:
    """Drain ``queue`` into ``buffer`` until ``predicate`` matches an event."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while True:
        while not queue.empty():
            buffer.append(queue.get_nowait())
        for index, event in enumerate(buffer):
            if predicate(event):
                return buffer.pop(index)
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("Timeout waiting for event")
        await asyncio.sleep(0.01)


def _is_countdown(event: Any) -> bool:
    return isinstance(event, SessionError) and "retrying in" in event.message


def _countdown_predicate(event: Any) -> bool:
    return _is_countdown(event)


@pytest.mark.asyncio
async def test_cascade_order_then_backoff(
    settings: AppSettings,
    command_queue: asyncio.Queue[Any],
    event_queue: asyncio.Queue[Any],
    journal: Journal,
    store: Store,
    tier_manager: TierManager,
    monkeypatch: Any,
) -> None:
    """429 -> same model/other tier -> fallback/same tier -> fallback/other -> backoff."""
    live = FakeLive()
    keys: list[str | None] = []
    models: list[str] = []
    attempts = 0
    original_connect = live.connect

    @contextlib.asynccontextmanager
    async def connect_wrapper(
        *, model: str, config: types.LiveConnectConfig | None = None
    ) -> Any:
        nonlocal attempts
        attempts += 1
        models.append(model)
        if attempts <= 4:
            raise _quota_error()
        async with original_connect(model=model, config=config) as session:
            yield session

    monkeypatch.setattr(live, "connect", connect_wrapper)
    _patch_common(monkeypatch, live, keys)

    supervisor = Supervisor(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=store,
        run_id="cascade-run",
        tier_manager=tier_manager,
    )
    task = asyncio.create_task(supervisor.run())

    events: list[Any] = []
    await _wait_for_event(
        event_queue,
        events,
        lambda e: isinstance(e, ConnectionStateChanged)
        and e.state == ConnectionState.LIVE,
    )

    # Cascade order: (tier1, M) -> (free, M) -> (tier1, F) -> (free, F), then
    # after the backoff the cascade restarts at the primary pool (tier1, M).
    assert keys == [TIER1_KEY, FREE_KEY, TIER1_KEY, FREE_KEY, TIER1_KEY]
    assert models == [MODEL, MODEL, FALLBACK, FALLBACK, MODEL]

    # Exactly 4 reconnection attempts happen before the first backoff tick.
    first_countdown = next(i for i, e in enumerate(events) if _is_countdown(e))
    hits_before = [e for e in events[:first_countdown] if isinstance(e, RateLimitHit)]
    assert len(hits_before) == 4

    # RateLimitHit events were enriched with the supervisor's tier tracking.
    assert [h.tier for h in hits_before] == ["tier1", "free", "tier1", "free"]
    assert [h.model for h in hits_before] == [MODEL, MODEL, FALLBACK, FALLBACK]
    assert hits_before[0].tpm_limit == TIER1_TPM[MODEL]
    assert hits_before[2].tpm_limit == TIER1_TPM[FALLBACK]

    await command_queue.put(Disconnect())
    reason = await asyncio.wait_for(task, timeout=5)
    assert reason.code == "user_request"


@pytest.mark.asyncio
async def test_resumption_handle_survives_tier_switch(
    settings: AppSettings,
    command_queue: asyncio.Queue[Any],
    event_queue: asyncio.Queue[Any],
    journal: Journal,
    store: Store,
    tier_manager: TierManager,
    monkeypatch: Any,
) -> None:
    """A reconnect after a 429 presents the last resumption handle on the new tier."""
    live = FakeLive()
    keys: list[str | None] = []
    live.grant_next_handle("handle-1")

    async def on_connect(session: FakeLiveSession) -> None:
        if len(live.sessions) == 1:
            # End epoch 1 via go_away, then make the immediate reconnect hit a
            # 429 so epoch 3 runs on the Free key.
            live.fail_next_connect(_quota_error())
            session.go_away("60s")
        else:
            session.say("back", turn_complete=True)

    live.on_connect(on_connect)
    _patch_common(monkeypatch, live, keys)

    supervisor = Supervisor(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=store,
        run_id="handle-run",
        tier_manager=tier_manager,
    )
    task = asyncio.create_task(supervisor.run())

    events: list[Any] = []
    # First LIVE (epoch 1), then a second LIVE (epoch 3, after the 429).
    await _wait_for_event(
        event_queue,
        events,
        lambda e: isinstance(e, ConnectionStateChanged)
        and e.state == ConnectionState.LIVE,
    )
    await _wait_for_event(
        event_queue,
        events,
        lambda e: isinstance(e, ConnectionStateChanged)
        and e.state == ConnectionState.LIVE,
    )

    # Epoch 2's connect raised, so only two fake sessions exist.
    assert len(live.sessions) == 2
    assert keys == [TIER1_KEY, TIER1_KEY, FREE_KEY]
    resumption = live.sessions[1].config.session_resumption
    assert resumption is not None
    assert resumption.handle == "handle-1"

    await command_queue.put(Disconnect())
    reason = await asyncio.wait_for(task, timeout=5)
    assert reason.code == "user_request"


@pytest.mark.asyncio
async def test_usage_recorded_against_current_pool(
    settings: AppSettings,
    command_queue: asyncio.Queue[Any],
    event_queue: asyncio.Queue[Any],
    journal: Journal,
    store: Store,
    monkeypatch: Any,
) -> None:
    """UsageUpdate deltas feed the TierManager rolling TPM tracker."""
    clock = [1000.0]
    tier_manager = TierManager(tier1_key=TIER1_KEY, free_key=FREE_KEY, clock=lambda: clock[0])
    tier1 = tier_manager.get_tier("tier1")
    assert tier1 is not None
    headroom_before = tier_manager.tpm_headroom(tier1, MODEL)

    live = FakeLive()
    keys: list[str | None] = []

    async def on_connect(session: FakeLiveSession) -> None:
        # Live API usage_metadata totals are cumulative within a session; the
        # supervisor must record only the delta (120, then +30).
        session.usage(prompt_token_count=100, response_token_count=20, total_token_count=120)
        session.usage(prompt_token_count=120, response_token_count=30, total_token_count=150)

    live.on_connect(on_connect)
    _patch_common(monkeypatch, live, keys)

    supervisor = Supervisor(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=store,
        run_id="usage-run",
        tier_manager=tier_manager,
    )
    task = asyncio.create_task(supervisor.run())

    events: list[Any] = []
    await _wait_for_event(
        event_queue,
        events,
        lambda e: isinstance(e, UsageUpdate) and e.total_tokens == 150,
    )
    await asyncio.sleep(0.05)  # let the tap process the second event

    assert tier_manager.tpm_headroom(tier1, MODEL) == headroom_before - 150
    assert tier1.tpm_used == 150

    await command_queue.put(Disconnect())
    reason = await asyncio.wait_for(task, timeout=5)
    assert reason.code == "user_request"


@pytest.mark.asyncio
async def test_disconnect_during_backoff_ends_run(
    settings: AppSettings,
    command_queue: asyncio.Queue[Any],
    event_queue: asyncio.Queue[Any],
    journal: Journal,
    store: Store,
    tier_manager: TierManager,
    monkeypatch: Any,
) -> None:
    """A Disconnect command is honored during the all-pools backoff countdown."""
    live = FakeLive()
    keys: list[str | None] = []

    @contextlib.asynccontextmanager
    async def always_quota(
        *, model: str, config: types.LiveConnectConfig | None = None
    ) -> Any:
        raise _quota_error()
        yield  # pragma: no cover - make this an async context manager

    monkeypatch.setattr(live, "connect", always_quota)
    _patch_common(monkeypatch, live, keys)

    supervisor = Supervisor(
        settings=settings,
        command_queue=command_queue,
        event_queue=event_queue,
        journal=journal,
        store=store,
        run_id="backoff-run",
        tier_manager=tier_manager,
    )
    task = asyncio.create_task(supervisor.run())

    events: list[Any] = []
    await _wait_for_event(event_queue, events, _countdown_predicate)

    await command_queue.put(Disconnect())
    reason = await asyncio.wait_for(task, timeout=5)
    assert reason.code == "user_request"

    # The 4 pools were tried before the first backoff began.
    assert keys[:4] == [TIER1_KEY, FREE_KEY, TIER1_KEY, FREE_KEY]
