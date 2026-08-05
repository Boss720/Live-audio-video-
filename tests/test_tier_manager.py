"""Unit tests for ``core.tier_manager``."""

from __future__ import annotations

import pytest

from core.tier_manager import DEFAULT_MODEL, TierManager

MODEL = DEFAULT_MODEL  # "gemini-3.1-flash-live-preview"


class FakeClock:
    """Controllable monotonic clock for the sliding-window tracker."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def manager(clock: FakeClock) -> TierManager:
    return TierManager(tier1_key="tier1-key", free_key="free-key", clock=clock)


def test_privacy_always_uses_tier1(manager: TierManager) -> None:
    tier = manager.select_tier(needs_privacy=True)
    assert tier.tier_name == "tier1"
    assert tier.is_private
    assert tier.api_key == "tier1-key"


def test_privacy_without_tier1_key_raises(clock: FakeClock) -> None:
    manager = TierManager(tier1_key=None, free_key="free-key", clock=clock)
    with pytest.raises(ValueError, match="Tier 1"):
        manager.select_tier(needs_privacy=True)


def test_video_prefers_tier1_on_ties(manager: TierManager) -> None:
    tier = manager.select_tier(has_video=True)
    assert tier.tier_name == "tier1"


def test_video_uses_tier_with_more_headroom(manager: TierManager) -> None:
    # Burn most of Tier 1's headroom so Free has more left.
    tier1 = manager.select_tier(needs_privacy=True)
    manager.record_usage(tier1, MODEL, 100_000)
    tier = manager.select_tier(has_video=True)
    assert tier.tier_name == "free"


def test_video_falls_back_to_tier1_when_free_saturated(manager: TierManager) -> None:
    free = manager.select_tier()
    manager.record_usage(free, MODEL, 65_000)
    tier = manager.select_tier(has_video=True)
    assert tier.tier_name == "tier1"


def test_audio_only_prefers_free(manager: TierManager) -> None:
    tier = manager.select_tier()
    assert tier.tier_name == "free"
    assert not tier.is_private


def test_audio_only_uses_tier1_when_free_exhausted(manager: TierManager) -> None:
    free = manager.select_tier()
    manager.record_usage(free, MODEL, 65_000)
    assert manager.tpm_headroom(free, MODEL) == 0
    tier = manager.select_tier()
    assert tier.tier_name == "tier1"


def test_no_keys_at_all_raises(clock: FakeClock) -> None:
    manager = TierManager(clock=clock)
    with pytest.raises(ValueError, match="No API key"):
        manager.select_tier()


def test_headroom_tracks_recent_usage(manager: TierManager) -> None:
    free = manager.select_tier()
    assert manager.tpm_headroom(free, MODEL) == 65_000
    manager.record_usage(free, MODEL, 15_000)
    assert manager.tpm_headroom(free, MODEL) == 50_000
    assert free.tpm_used == 15_000


def test_window_ignores_entries_older_than_60s(
    manager: TierManager, clock: FakeClock
) -> None:
    free = manager.select_tier()
    manager.record_usage(free, MODEL, 65_000)
    assert manager.tpm_headroom(free, MODEL) == 0

    clock.advance(61.0)
    # The saturated entry aged out: full headroom and Free routing are back.
    assert manager.tpm_headroom(free, MODEL) == 65_000
    assert manager.select_tier().tier_name == "free"


def test_window_prunes_only_expired_entries(
    manager: TierManager, clock: FakeClock
) -> None:
    free = manager.select_tier()
    manager.record_usage(free, MODEL, 40_000)
    clock.advance(31.0)
    manager.record_usage(free, MODEL, 10_000)

    clock.advance(31.0)  # first entry (t=0) expires; second (t=31) remains
    assert manager.tpm_headroom(free, MODEL) == 65_000 - 10_000

    manager.reset_if_needed()
    assert free.tpm_used == 10_000


def test_usage_is_tracked_per_tier_and_model(manager: TierManager) -> None:
    free = manager.select_tier()
    manager.record_usage(free, MODEL, 65_000)
    # A different model on the same tier is unaffected.
    assert manager.tpm_headroom(free, "gemini-2.5-flash-native-audio-preview-12-2025") == 1_000_000
    tier1 = manager.select_tier(needs_privacy=True)
    assert manager.tpm_headroom(tier1, MODEL) == 150_000
