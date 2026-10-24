"""Runtime measurement and usage integration without a provider or devices."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from audit.journal import Journal
from core.events import UsageUpdate
from core.session import LiveSession
from persist.store import Store
from settings.settings import AppSettings


def test_usage_modalities_share_one_transaction(tmp_path: Path) -> None:
    store = Store(db_path=tmp_path / "metrics.db")
    journal = Journal(run_id="metrics", log_dir=tmp_path)
    settings = AppSettings(_env_file=None, working_dir=tmp_path, workspace_root=tmp_path)
    session = LiveSession(
        settings, asyncio.Queue(), asyncio.Queue(), journal, store, "metrics"
    )
    statements: list[str] = []
    store._write_conn.set_trace_callback(statements.append)
    try:
        session._record_usage(UsageUpdate(
            prompt_tokens=10, cached_tokens=2, completion_tokens=3,
            audio_tokens=4, video_tokens=5, estimated_usd=0.01,
        ))
        store.flush()
        assert sum(sql.startswith("BEGIN") for sql in statements) == 1
        assert sum(sql.startswith("COMMIT") for sql in statements) == 1
        assert [(row[0], row[1]) for row in store.get_usage("metrics")] == [
            ("prompt", 10), ("cached", 2), ("completion", 3), ("audio", 4), ("video", 5),
        ]
    finally:
        journal.close()
        store.close()


def test_timing_samples_and_metric_names_are_bounded() -> None:
    from obs.metrics import RuntimeMetrics

    metrics = RuntimeMetrics(max_samples=3, max_series=2)
    for value in range(10):
        metrics.record_ms("audio_send_ms", float(value))
    summary = metrics.snapshot()["audio_send_ms"]
    assert summary.samples == 3
    assert summary.p50_ms == 8.0
    assert summary.p95_ms == 9.0
    assert summary.latest_ms == 9.0
    metrics.record_ms("loop_lag_ms", 1.0)
    with pytest.raises(ValueError, match="series"):
        metrics.record_ms("extra", 1.0)
    assert len(metrics.snapshot()) == 2
    with pytest.raises(ValueError):
        metrics.record_ms("loop_lag_ms", float("nan"))


def test_timing_context_records_elapsed_without_payload() -> None:
    from obs.metrics import RuntimeMetrics

    now = [1.0]
    metrics = RuntimeMetrics(clock=lambda: now[0])
    with metrics.measure("storage_ms"):
        now[0] += 0.012
    assert metrics.snapshot()["storage_ms"].latest_ms == pytest.approx(12.0)


def test_mic_chunk_settings_preserve_default_and_reject_bad_choices() -> None:
    assert AppSettings(_env_file=None).audio_chunk_duration_ms == 100
    assert AppSettings(_env_file=None, audio_chunk_duration_ms=20).audio_chunk_duration_ms == 20
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, audio_chunk_duration_ms=21)


@pytest.mark.asyncio
async def test_audio_send_timing_and_snapshot_with_minimal_media_fakes(tmp_path: Path) -> None:
    from obs.metrics import RuntimeMetrics

    now = [1.0]

    class Mic:
        diagnostics = {"queued_ms": 40.0, "dropped_chunks": 1.0}

        async def chunks(self) -> AsyncIterator[bytes]:
            yield bytes(16)

    class Socket:
        async def send_realtime_input(self, **kwargs: Any) -> None:
            now[0] += 0.005

    store = Store(db_path=tmp_path / "audio.db")
    journal = Journal(run_id="timing", log_dir=tmp_path)
    session = LiveSession(
        AppSettings(_env_file=None), asyncio.Queue(), asyncio.Queue(), journal, store, "timing"
    )
    session._metrics = RuntimeMetrics(clock=lambda: now[0])
    mic = Mic()
    session._mic = mic  # type: ignore[assignment]
    try:
        await session._audio_send_loop(Socket(), mic)  # type: ignore[arg-type]
        snapshot = session.runtime_diagnostics()
        assert snapshot.timings["audio_send_ms"].latest_ms == pytest.approx(5.0)
        assert snapshot.audio["mic.queued_ms"] == 40.0
        assert snapshot.audio["mic.dropped_chunks"] == 1.0
        assert snapshot.counters == {"audio_chunks_sent": 1, "audio_bytes_sent": 16}
    finally:
        journal.close()
        store.close()
