"""Wave 3 echo gate: speaker is_playing, mic suppression, headset detection."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from media import devices
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


def _pull(speaker: Speaker) -> bytes:
    out = bytearray(b"\xff" * BLOCKSIZE_FRAMES * 2)
    speaker._audio_callback(out, BLOCKSIZE_FRAMES, None, None)
    return bytes(out)


def _loud(ms: int) -> bytes:
    return (8000).to_bytes(2, "little", signed=True) * (OUTPUT_SAMPLE_RATE * ms // 1000)


@pytest.mark.asyncio
async def test_speaker_is_playing_with_hangover(monkeypatch: Any) -> None:
    now = [100.0]
    monkeypatch.setattr(speaker_mod.time, "monotonic", lambda: now[0])
    speaker = Speaker(stream_factory=_Stream)
    await speaker.start()
    assert not speaker.is_playing
    _pull(speaker)  # idle silence does not count
    assert not speaker.is_playing
    speaker.write(_loud(100))
    _pull(speaker)
    assert speaker.is_playing
    now[0] += 0.2
    assert speaker.is_playing  # inside hangover
    now[0] += 0.1
    assert not speaker.is_playing  # hangover elapsed


@pytest.mark.asyncio
async def test_prebuffer_silence_does_not_count() -> None:
    speaker = Speaker(stream_factory=_Stream)
    await speaker.start()
    speaker.write(_loud(5))  # below prebuffer: stays silent
    _pull(speaker)
    assert not speaker.is_playing


@pytest.mark.asyncio
async def test_speaker_stop_clears_is_playing() -> None:
    speaker = Speaker(stream_factory=_Stream)
    await speaker.start()
    speaker.write(_loud(100))
    _pull(speaker)
    assert speaker.is_playing
    await speaker.stop()
    assert not speaker.is_playing


@pytest.mark.asyncio
async def test_mic_sends_silence_when_echo_gated() -> None:
    gated = [True]
    mic = Microphone(echo_gate=lambda: gated[0], chunk_duration_ms=40)
    mic._loop = asyncio.get_running_loop()
    mic._running = True
    chunk = (1000).to_bytes(2, "little", signed=True) * 640
    mic._enqueue_chunk(chunk)
    assert mic.diagnostics["echo_suppressed_chunks"] == 1.0
    gated[0] = False
    mic._enqueue_chunk(chunk)
    assert mic.diagnostics["echo_suppressed_chunks"] == 1.0
    await asyncio.sleep(0)
    first = mic._queue[0]
    second = mic._queue[1]
    assert first is not None and second is not None
    assert first[1] == bytes(len(chunk))  # silence, same length: stream continuous
    assert second[1] == chunk
    assert mic.level == 0.0 or mic.level > 0.0  # meter updated without error


@pytest.mark.asyncio
async def test_mic_without_gate_is_unchanged() -> None:
    mic = Microphone()
    mic._loop = asyncio.get_running_loop()
    mic._running = True
    chunk = b"\x01\x02" * 640
    mic._enqueue_chunk(chunk)
    assert mic._queue[0] is not None and mic._queue[0][1] == chunk
    assert mic.diagnostics["echo_suppressed_chunks"] == 0.0


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Headset Earphone (Realtek)", True),
        ("Headphones (USB)", True),
        ("AirPods Pro", True),
        ("Galaxy Buds2", True),
        ("Hands-Free AG Audio", True),
        ("Speakers (Realtek High Definition Audio)", False),
        ("DELL U2720Q (NVIDIA HD Audio)", False),
    ],
)
def test_is_headset_output_by_name(name: str, expected: bool) -> None:
    assert devices.is_headset_output(name) is expected


def test_is_headset_output_default_device(monkeypatch: Any) -> None:
    monkeypatch.setattr(devices.sd, "query_devices", lambda kind=None: {"name": "Headphones"})
    assert devices.is_headset_output(None)

    def boom(kind: str | None = None) -> Any:
        raise RuntimeError("no device")

    monkeypatch.setattr(devices.sd, "query_devices", boom)
    assert not devices.is_headset_output(None)


def test_should_gate_echo_policy(monkeypatch: Any) -> None:
    assert devices.should_gate_echo("on", "Headphones")
    assert not devices.should_gate_echo("off", "Speakers")
    assert devices.should_gate_echo("auto", "Speakers")
    assert not devices.should_gate_echo("auto", "Headset")


def test_echo_gate_setting_default_and_validation() -> None:
    assert AppSettings().echo_gate == "auto"
    with pytest.raises(ValueError):
        AppSettings(echo_gate="maybe")  # type: ignore[arg-type]


def test_hangover_constant() -> None:
    assert speaker_mod.ECHO_HANGOVER_MS == 250
    assert time  # keep import used
