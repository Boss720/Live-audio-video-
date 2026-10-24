"""Privacy-mode / prefer_free_tier routing in the Supervisor."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from audit.journal import Journal
from core.events import ConnectionState, ConnectionStateChanged, SessionError
from core.supervisor import Supervisor
from core.tier_manager import TierManager
from persist.store import Store
from settings.settings import AppSettings
from tests.fakes.fake_live import FakeLive
from tests.test_supervisor import _patch_for_supervisor, drain

MODEL = "gemini-3.1-flash-live-preview"
FALLBACK = "gemini-2.5-flash-native-audio-preview-12-2025"


@pytest.fixture
def journal(tmp_path: Path) -> Generator[Journal, None, None]:
    j = Journal(run_id="priv", log_dir=tmp_path)
    yield j
    j._close()


def _make(
    tmp_path: Path,
    journal: Journal,
    *,
    privacy: str,
    prefer_free: bool = True,
    tier1: str | None = "t1",
    free: str | None = "fr",
) -> tuple[Supervisor, asyncio.Queue[Any]]:
    settings = AppSettings(
        model=MODEL,
        fallback_model=FALLBACK,
        mode="text",
        working_dir=tmp_path,
        workspace_root=tmp_path,
        wake_word_enabled=False,
        computer_use_enabled=False,
        share_screen=False,
        privacy_mode=privacy,
        prefer_free_tier=prefer_free,
    )
    events: asyncio.Queue[Any] = asyncio.Queue()
    sup = Supervisor(
        settings=settings,
        command_queue=asyncio.Queue(),
        event_queue=events,
        journal=journal,
        store=Store(db_path=tmp_path / "s.db"),
        run_id="priv",
        tier_manager=TierManager(tier1_key=tier1, free_key=free),
    )
    return sup, events


def _name(sup: Supervisor) -> str | None:
    return sup._current_tier.tier_name if sup._current_tier else None


def test_strict_starts_on_tier1(tmp_path: Path, journal: Journal) -> None:
    sup, _ = _make(tmp_path, journal, privacy="strict", prefer_free=True)
    assert _name(sup) == "tier1"


def test_strict_cascade_never_leaves_tier1(tmp_path: Path, journal: Journal) -> None:
    sup, _ = _make(tmp_path, journal, privacy="strict")
    sup._cascade_anchor = (sup._current_tier, MODEL)  # type: ignore[assignment]
    sup._exhausted_pools.add(("tier1", MODEL))
    nxt = sup._next_pool()
    assert nxt is not None and nxt[0].tier_name == "tier1" and nxt[1] == FALLBACK
    sup._exhausted_pools.add(("tier1", FALLBACK))
    assert sup._next_pool() is None


def test_strict_reset_stays_on_tier1(tmp_path: Path, journal: Journal) -> None:
    sup, _ = _make(tmp_path, journal, privacy="strict")
    sup._current_tier = sup._tier_manager.get_tier("free")  # type: ignore[union-attr]
    sup._reset_primary_pool()
    assert _name(sup) == "tier1"


@pytest.mark.asyncio
async def test_strict_free_only_fails_fatally(
    tmp_path: Path, journal: Journal, monkeypatch: Any
) -> None:
    live = FakeLive()
    _patch_for_supervisor(monkeypatch, live)
    sup, events = _make(tmp_path, journal, privacy="strict", tier1=None, free="fr")
    reason = await asyncio.wait_for(sup.run(), timeout=2)
    assert reason.code == "auth_error"
    assert live.sessions == []
    items = await drain(events)
    errs = [e for e in items if isinstance(e, SessionError)]
    assert errs and errs[0].fatal and "Tier 1" in errs[0].message
    assert any(
        isinstance(e, ConnectionStateChanged) and e.state == ConnectionState.FAILED
        for e in items
    )


@pytest.mark.parametrize("privacy", ["relaxed", "auto"])
def test_prefer_free_starts_on_free(
    tmp_path: Path, journal: Journal, privacy: str
) -> None:
    sup, _ = _make(tmp_path, journal, privacy=privacy, prefer_free=True)
    assert _name(sup) == "free"


@pytest.mark.parametrize("privacy", ["relaxed", "auto"])
def test_prefer_paid_starts_on_tier1(
    tmp_path: Path, journal: Journal, privacy: str
) -> None:
    sup, _ = _make(tmp_path, journal, privacy=privacy, prefer_free=False)
    assert _name(sup) == "tier1"


def test_prefer_free_without_free_key_uses_tier1(
    tmp_path: Path, journal: Journal
) -> None:
    sup, _ = _make(tmp_path, journal, privacy="auto", free=None)
    assert _name(sup) == "tier1"


@pytest.mark.parametrize("privacy", ["relaxed", "auto"])
def test_free_only_key_starts_on_free(
    tmp_path: Path, journal: Journal, privacy: str
) -> None:
    sup, _ = _make(tmp_path, journal, privacy=privacy, tier1=None)
    assert _name(sup) == "free"
    assert sup._routing_error is None


def test_relaxed_fallback_crosses_tiers_and_reset_honours_preference(
    tmp_path: Path, journal: Journal
) -> None:
    sup, _ = _make(tmp_path, journal, privacy="relaxed", prefer_free=True)
    sup._cascade_anchor = (sup._current_tier, MODEL)  # type: ignore[assignment]
    sup._exhausted_pools.add(("free", MODEL))
    nxt = sup._next_pool()
    assert nxt is not None and nxt[0].tier_name == "tier1"
    sup._current_tier = nxt[0]
    sup._reset_primary_pool()
    assert _name(sup) == "free"
