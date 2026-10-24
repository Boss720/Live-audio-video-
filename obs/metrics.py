"""Bounded, content-free measurements for one asyncio session.

Percentiles describe the most recent samples, not provider compute time. This
collector belongs to the event-loop thread; media threads expose separate audio
snapshots. No file writes, background threads, or message payloads are retained.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TimingSummary:
    samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    latest_ms: float
    max_ms: float


class RuntimeMetrics:
    """Keep fixed-size sample windows and a bounded set of counter names."""

    def __init__(
        self,
        *,
        max_samples: int = 256,
        max_series: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_samples < 1 or max_series < 1:
            raise ValueError("Metric sample and series limits must be positive")
        self._max_samples = max_samples
        self._max_series = max_series
        self._clock = clock
        self._samples: dict[str, deque[float]] = {}
        self._counters: dict[str, int] = {}

    def stamp(self) -> float:
        return self._clock()

    def record_ms(self, name: str, milliseconds: float) -> None:
        if not math.isfinite(milliseconds) or milliseconds < 0:
            raise ValueError("Timing samples must be finite and nonnegative")
        if name not in self._samples:
            if len(self._samples) >= self._max_series:
                raise ValueError("Metric series limit exceeded")
            self._samples[name] = deque(maxlen=self._max_samples)
        self._samples[name].append(milliseconds)

    def record_elapsed(self, name: str, started_at: float) -> None:
        self.record_ms(name, max(0.0, self.stamp() - started_at) * 1000)

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        started_at = self.stamp()
        try:
            yield
        finally:
            self.record_elapsed(name, started_at)

    def increment(self, name: str, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("Counter increments must be nonnegative")
        if name not in self._counters and len(self._counters) >= self._max_series:
            raise ValueError("Counter series limit exceeded")
        self._counters[name] = self._counters.get(name, 0) + amount

    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    def snapshot(self) -> dict[str, TimingSummary]:
        result = {}
        for name, samples in self._samples.items():
            ordered = sorted(samples)

            def percentile(fraction: float, values: list[float] = ordered) -> float:
                return values[math.ceil(len(values) * fraction) - 1]

            result[name] = TimingSummary(
                len(samples), percentile(0.5), percentile(0.95), percentile(0.99),
                samples[-1], ordered[-1],
            )
        return result
