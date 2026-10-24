"""Wave 2 latency: mic low-latency/40 ms, speaker prebuffer, compression trigger."""

from __future__ import annotations

from typing import Any

import pytest

from media import speaker as speaker_mod
from media.microphone import Microphone
from media.speaker import BLOCKSIZE_FRAMES, OUTPUT_SAMPLE_RATE, Speaker
from settings.settings import AppSettings


class _Stream:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


def _pull(speaker: Speaker, frames: int = BLOCKSIZE_FRAMES) -> bytes:
    out = bytearray(b"\xff" * frames * 2)
    speaker._audio_callback(out, frames, None, None)
    return bytes(out)


def _ms(ms: int, value: int = 1) -> bytes:
    return value.to_bytes(2, "little") * (OUTPUT_SAMPLE_RATE * ms // 1000)


@pytest.mark.asyncio
async def test_mic_stream_is_low_latency_and_default_chunk_is_40ms(monkeypatch: Any) -> None:
    streams: list[_Stream] = []

    def factory(**kwargs: Any) -> _Stream:
        streams.append(_Stream(**kwargs))
        return streams[-1]

    monkeypatch.setattr("media.microphone.sd.RawInputStream", factory)
    mic = Microphone(1)
    await mic.start()
    try:
        assert streams[0].kwargs["latency"] == "low"
        assert streams[0].kwargs["blocksize"] == 640  # 40 ms at 16 kHz
        assert mic.diagnostics["chunk_duration_ms"] == 40.0
    finally:
        await mic.stop()


def test_mic_chunk_setting_default_is_40_and_still_validated() -> None:
    assert AppSettings(_env_file=None).audio_chunk_duration_ms == 40
    assert AppSettings(_env_file=None, audio_chunk_duration_ms=100).audio_chunk_duration_ms == 100
    with pytest.raises(ValueError):
        AppSettings(_env_file=None, audio_chunk_duration_ms=30)


def test_trigger_tokens_defaults_below_nothing_but_above_targets() -> None:
    settings = AppSettings(_env_file=None)
    assert settings.trigger_tokens == 100_000
    assert settings.target_tokens < settings.trigger_tokens
    assert settings.target_tokens_tier1 < settings.trigger_tokens
    assert AppSettings(_env_file=None, trigger_tokens=None).trigger_tokens is None


def test_speaker_stream_uses_fixed_20ms_blocksize() -> None:
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> _Stream:
        captured.update(kwargs)
        return _Stream()

    import asyncio

    asyncio.run(Speaker(stream_factory=factory).start())
    assert captured["blocksize"] == BLOCKSIZE_FRAMES == 480
    assert captured["latency"] == "low"


def test_prebuffer_holds_audio_until_40ms_queued() -> None:
    speaker = Speaker(stream_factory=_Stream)
    speaker._active = True
    speaker.write(_ms(20))
    assert _pull(speaker) == bytes(BLOCKSIZE_FRAMES * 2)  # still buffering
    assert speaker.diagnostics["played_ms"] == 0.0
    speaker.write(_ms(20))
    assert _pull(speaker) == _ms(20)  # 40 ms queued: playback starts
    assert _pull(speaker) == _ms(20)


def test_short_tail_is_not_stranded_by_prebuffer() -> None:
    speaker = Speaker(stream_factory=_Stream)
    speaker._active = True
    speaker.write(_ms(10))
    played = b"".join(_pull(speaker) for _ in range(6))
    assert _ms(10) in played


def test_underrun_rearms_prebuffer() -> None:
    speaker = Speaker(stream_factory=_Stream)
    speaker._active = True
    speaker.write(_ms(40))
    assert _pull(speaker) == _ms(20)
    assert _pull(speaker) == _ms(20)
    _pull(speaker)  # underrun
    speaker.write(_ms(20))
    assert _pull(speaker) == bytes(BLOCKSIZE_FRAMES * 2)
    speaker.write(_ms(20))
    assert _pull(speaker) == _ms(20)


def test_flush_rearms_prebuffer_and_clears_level() -> None:
    speaker = Speaker(stream_factory=_Stream)
    speaker._active = True
    speaker.write(_ms(60, 8000))
    _pull(speaker)
    assert speaker.level > 0.0
    speaker.flush()
    assert speaker.level == 0.0
    speaker.write(_ms(20))
    assert _pull(speaker) == bytes(BLOCKSIZE_FRAMES * 2)


def test_level_is_computed_in_callback_from_played_bytes() -> None:
    speaker = Speaker(stream_factory=_Stream)
    speaker._active = True
    speaker.write(_ms(40, 16384))
    assert speaker.level == 0.0  # write() no longer computes the level
    _pull(speaker)
    assert speaker.level == pytest.approx(0.5, abs=0.01)
    _pull(speaker)
    _pull(speaker)  # silence: level drops to zero
    assert speaker.level == 0.0


def test_prebuffer_constant_is_40ms() -> None:
    assert speaker_mod.PREBUFFER_MS == 40
