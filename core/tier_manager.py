"""Dual-tier API key routing and rolling TPM rate-limit tracking.

The operator may hold two Gemini API keys: a Tier 1 (paid) key and a Free
tier key. ``TierManager`` decides which key a session should use based on
privacy requirements, modality (video burns TPM far faster than audio), and
each tier's remaining tokens-per-minute headroom in a rolling 60-second
window. The limit numbers come from the AI Studio dashboard; RPM/RPD are
unlimited on both tiers for every supported model, so only TPM is tracked.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from obs.logging_setup import get_logger

logger = get_logger(__name__)

# Matches ``settings.settings.AppSettings.model``; duplicated here so this
# module stays free of the pydantic/genai import chain.
DEFAULT_MODEL = "gemini-3.1-flash-live-preview"

_WINDOW_SECONDS = 60.0
_UNLIMITED = float("inf")


@dataclass(frozen=True, slots=True)
class ModelTierLimits:
    """Per-model rate limits for one tier.

    ``rpm_limit``/``rpd_limit`` are ``float('inf')`` when the tier imposes no
    cap, which is the case for all currently supported models.
    """

    tpm_limit: int
    rpm_limit: float = _UNLIMITED
    rpd_limit: float = _UNLIMITED


@dataclass(slots=True)
class TierConfig:
    """One configured API tier: its key, limits, and live usage counters.

    ``tpm_used`` and ``last_reset`` are maintained by ``TierManager`` as a
    convenience snapshot of the sliding-window tracker; the deque of usage
    events inside the manager is the source of truth.
    """

    api_key: str
    tier_name: str
    is_private: bool
    limits: dict[str, ModelTierLimits]
    tpm_used: int = 0
    last_reset: float = 0.0


class TierManager:
    """Route sessions between the Tier 1 and Free API keys by TPM headroom.

    Parameters
    ----------
    tier1_key:
        The paid (Tier 1) API key, or ``None`` if not configured. Required for
        privacy-sensitive traffic.
    free_key:
        The Free tier API key, or ``None`` if not configured.
    clock:
        Monotonic clock used by the sliding-window tracker. Injectable so
        tests can advance time without sleeping.
    """

    TIER1_LIMITS: dict[str, ModelTierLimits] = {
        "gemini-2.5-flash-native-audio-preview-12-2025": ModelTierLimits(tpm_limit=1_000_000),
        "gemini-3.1-flash-live-preview": ModelTierLimits(tpm_limit=150_000),
        "gemini-3.5-live-translate-preview": ModelTierLimits(tpm_limit=100_000),
        "gemini-3.8-live": ModelTierLimits(tpm_limit=150_000),
        "gemini-3.8-live-extended-thinking": ModelTierLimits(tpm_limit=150_000),
    }
    FREE_LIMITS: dict[str, ModelTierLimits] = {
        "gemini-2.5-flash-native-audio-preview-12-2025": ModelTierLimits(tpm_limit=1_000_000),
        "gemini-3.1-flash-live-preview": ModelTierLimits(tpm_limit=65_000),
        "gemini-3.5-live-translate-preview": ModelTierLimits(tpm_limit=20_000),
        "gemini-3.8-live": ModelTierLimits(tpm_limit=65_000),
        "gemini-3.8-live-extended-thinking": ModelTierLimits(tpm_limit=65_000),
    }

    def __init__(
        self,
        tier1_key: str | None = None,
        free_key: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._tiers: dict[str, TierConfig] = {}
        if tier1_key:
            self._tiers["tier1"] = TierConfig(
                api_key=tier1_key,
                tier_name="tier1",
                is_private=True,
                limits=dict(self.TIER1_LIMITS),
            )
        if free_key:
            self._tiers["free"] = TierConfig(
                api_key=free_key,
                tier_name="free",
                is_private=False,
                limits=dict(self.FREE_LIMITS),
            )
        # (tier_name, model) -> deque of (timestamp, tokens), oldest first.
        self._usage: dict[tuple[str, str], deque[tuple[float, int]]] = {}

    def select_tier(
        self,
        needs_privacy: bool = False,
        has_video: bool = False,
        model: str = DEFAULT_MODEL,
    ) -> TierConfig:
        """Pick the tier a session should use.

        Privacy-sensitive traffic must use the Tier 1 key; like
        ``LiveSession.run`` we raise ``ValueError`` when the required key is
        not configured rather than silently downgrading to the Free key.
        Video traffic goes to whichever tier has more TPM headroom (Tier 1
        wins ties); audio-only traffic prefers Free to save paid quota.
        """
        tier1 = self._tiers.get("tier1")
        free = self._tiers.get("free")

        if needs_privacy:
            if tier1 is None:
                raise ValueError(
                    "Privacy-sensitive request but no Tier 1 API key configured"
                )
            return tier1

        if has_video:
            candidates = [t for t in (tier1, free) if t is not None]
            if not candidates:
                raise ValueError("No API key configured")
            # Tier 1 is listed first, so max() prefers it on headroom ties.
            return max(candidates, key=lambda t: self.tpm_headroom(t, model))

        if free is not None and self.tpm_headroom(free, model) > 0:
            return free
        if tier1 is not None:
            return tier1
        if free is not None:
            # Free is saturated but it is the only key we have.
            logger.warning("Free tier TPM exhausted; routing to it anyway (only key)")
            return free
        raise ValueError("No API key configured")

    def get_tier(self, tier_name: str) -> TierConfig | None:
        """Return the configured tier by name (``"tier1"``/``"free"``), or ``None``."""
        return self._tiers.get(tier_name)

    def record_usage(self, tier: TierConfig, model: str, tokens: int) -> None:
        """Record ``tokens`` consumed by ``tier`` on ``model`` at the current time."""
        if tokens <= 0:
            return
        key = (tier.tier_name, model)
        window = self._usage.setdefault(key, deque())
        window.append((self._clock(), tokens))
        self._prune(key)
        tier.tpm_used = sum(t for _, t in window)
        tier.last_reset = self._clock()

    def tpm_headroom(self, tier: TierConfig, model: str) -> int:
        """Return the tier's remaining TPM for ``model`` in the last 60 seconds."""
        key = (tier.tier_name, model)
        self._prune(key)
        used = sum(t for _, t in self._usage.get(key, ()))
        return max(0, self._limits_for(tier, model).tpm_limit - used)

    def reset_if_needed(self) -> None:
        """Drop usage events older than the rolling window on every tier/model."""
        for key in list(self._usage):
            self._prune(key)
        for tier in self._tiers.values():
            now = self._clock()
            tier.tpm_used = sum(
                tokens
                for (name, _model), window in self._usage.items()
                if name == tier.tier_name
                for _, tokens in window
            )
            if now - tier.last_reset >= _WINDOW_SECONDS:
                tier.last_reset = now

    def _limits_for(self, tier: TierConfig, model: str) -> ModelTierLimits:
        limits = tier.limits.get(model)
        if limits is None:
            # Unknown model: assume the default model's limits rather than
            # pretending the tier is uncapped.
            logger.warning(
                "No %s limits for model %r; falling back to %r",
                tier.tier_name,
                model,
                DEFAULT_MODEL,
            )
            limits = tier.limits[DEFAULT_MODEL]
        return limits

    def _prune(self, key: tuple[str, str]) -> None:
        """Drop entries older than ``_WINDOW_SECONDS`` from one usage window."""
        window = self._usage.get(key)
        if window is None:
            return
        cutoff = self._clock() - _WINDOW_SECONDS
        while window and window[0][0] <= cutoff:
            window.popleft()
        if not window:
            del self._usage[key]
