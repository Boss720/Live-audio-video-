"""Unit tests for the NDJSON stdio driver in ``bridge_stdio``."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from bridge_stdio import (
    _EOF,
    PROTOCOL_VERSION,
    _await_connect,
    _command_loop,
    _Writer,
    build_settings,
    encode_event,
    parse_command,
)
from core.commands import (
    ApproveTool,
    CancelTool,
    DenyTool,
    Disconnect,
    GatingMode,
    SendText,
    SetGatingMode,
    SetMicGate,
    SetShareScreen,
)
from core.events import (
    ConnectionState,
    ConnectionStateChanged,
    PartialTranscript,
    ToolApprovalRequested,
    TranscriptSource,
    UsageUpdate,
)


def _writer() -> tuple[_Writer, io.StringIO]:
    stream = io.StringIO()
    return _Writer(stream), stream


def _frames(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


# --- event encoding ---------------------------------------------------------


def test_encode_event_tags_snake_case_type() -> None:
    frame = encode_event(
        PartialTranscript(text="hi", source=TranscriptSource.MODEL, turn_id=3)
    )
    assert frame["type"] == "partial_transcript"
    assert frame["text"] == "hi"
    assert frame["turn_id"] == 3


def test_encode_event_flattens_enums_and_datetimes() -> None:
    stamp = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    frame = encode_event(
        ConnectionStateChanged(
            state=ConnectionState.LIVE, detail="ok", timestamp=stamp
        )
    )
    assert frame["state"] == "live"
    assert frame["timestamp"] == stamp.isoformat()
    # Must survive a real serialization round trip, not just look right.
    assert json.loads(json.dumps(frame))["state"] == "live"


def test_encode_event_keeps_nested_tool_args() -> None:
    frame = encode_event(
        ToolApprovalRequested(
            call_id="c1",
            name="write_file",
            args={"path": "a.txt", "lines": [1, 2]},
            allow_text="writes a.txt",
        )
    )
    assert frame["type"] == "tool_approval_requested"
    assert frame["args"] == {"path": "a.txt", "lines": [1, 2]}
    assert frame["allow_text"] == "writes a.txt"


def test_encode_event_preserves_none_cost() -> None:
    frame = encode_event(UsageUpdate(total_tokens=12, estimated_usd=None))
    assert frame["total_tokens"] == 12
    assert frame["estimated_usd"] is None


def test_writer_emits_one_line_per_frame() -> None:
    writer, stream = _writer()
    writer.emit({"type": "ready"})
    writer.emit({"type": "session_ended", "code": "user_request"})
    assert [frame["type"] for frame in _frames(stream)] == ["ready", "session_ended"]


# --- command parsing --------------------------------------------------------


def test_parse_command_covers_the_vocabulary() -> None:
    assert parse_command({"type": "disconnect"}) == Disconnect()
    assert parse_command({"type": "send_text", "text": "hello"}) == SendText(text="hello")
    assert parse_command({"type": "set_mic_gate", "open": False}) == SetMicGate(open=False)
    assert parse_command(
        {"type": "set_share_screen", "enabled": True, "monitor": 2}
    ) == SetShareScreen(enabled=True, monitor=2)
    assert parse_command({"type": "approve_tool", "call_id": "c1"}) == ApproveTool(
        call_id="c1"
    )
    assert parse_command({"type": "cancel_tool", "call_ids": ["a", "b"]}) == CancelTool(
        call_ids=["a", "b"]
    )


def test_parse_command_defaults_deny_reason() -> None:
    command = parse_command({"type": "deny_tool", "call_id": "c1"})
    assert command == DenyTool(call_id="c1", reason="user_denied")


def test_parse_command_reads_gating_mode_and_duration() -> None:
    command = parse_command(
        {"type": "set_gating_mode", "mode": "yolo", "duration_minutes": 15}
    )
    assert command == SetGatingMode(mode=GatingMode.YOLO, duration_minutes=15)


def test_parse_command_ignores_driver_owned_frames() -> None:
    assert parse_command({"type": "connect", "settings": {}}) is None
    assert parse_command({"type": "ping"}) is None


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "nonsense"},
        {"type": "send_text", "text": "   "},
        {"type": "approve_tool"},
        {"type": "cancel_tool", "call_ids": "not-a-list"},
        {"type": "set_gating_mode", "mode": "reckless"},
    ],
)
def test_parse_command_rejects_bad_frames(frame: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_command(frame)


# --- settings ---------------------------------------------------------------


def test_build_settings_drops_unknown_keys_and_forces_headless() -> None:
    settings = build_settings({"voice": "Kore", "not_a_real_setting": 1})
    assert settings.voice == "Kore"
    assert settings.headless is True


def test_build_settings_fills_tier_keys_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "tier1-from-env")
    monkeypatch.setenv("GOOGLE_API_KEY_FREE", "free-from-env")
    settings = build_settings({})
    assert settings.api_key_tier1 == "tier1-from-env"
    assert settings.api_key_free == "free-from-env"


def test_build_settings_keeps_explicit_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "tier1-from-env")
    settings = build_settings({"api_key_tier1": "explicit"})
    assert settings.api_key_tier1 == "explicit"


# --- loops ------------------------------------------------------------------


async def test_await_connect_returns_the_settings_payload() -> None:
    writer, _ = _writer()
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait(json.dumps({"type": "connect", "settings": {"voice": "Kore"}}))
    assert await _await_connect(queue, writer) == {"voice": "Kore"}


async def test_await_connect_reports_malformed_json_and_keeps_waiting() -> None:
    writer, stream = _writer()
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait("{not json")
    queue.put_nowait(json.dumps({"type": "connect", "settings": {}}))
    assert await _await_connect(queue, writer) == {}
    assert _frames(stream)[0]["type"] == "session_error"
    assert _frames(stream)[0]["fatal"] is False


async def test_await_connect_treats_disconnect_as_a_clean_exit() -> None:
    writer, _ = _writer()
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait(json.dumps({"type": "disconnect"}))
    assert await _await_connect(queue, writer) is None


async def test_command_loop_forwards_and_reports_without_dying() -> None:
    writer, stream = _writer()
    stdin_queue: asyncio.Queue[str] = asyncio.Queue()
    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    stdin_queue.put_nowait(json.dumps({"type": "bogus"}))
    stdin_queue.put_nowait(json.dumps({"type": "send_text", "text": "hi"}))

    task = asyncio.create_task(_command_loop(stdin_queue, command_queue, writer))
    command = await asyncio.wait_for(command_queue.get(), timeout=1)
    task.cancel()

    assert command == SendText(text="hi")
    assert _frames(stream)[0]["type"] == "session_error"


def test_protocol_version_is_pinned() -> None:
    # The host checks this in its `ready` handshake; bumping it is a breaking change.
    assert PROTOCOL_VERSION == 1


async def test_await_connect_ignores_a_liveness_ping() -> None:
    writer, stream = _writer()
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait(json.dumps({"type": "ping"}))
    queue.put_nowait(json.dumps({"type": "connect", "settings": {}}))
    assert await _await_connect(queue, writer) == {}
    assert _frames(stream) == []


async def test_await_connect_treats_eof_as_clean_exit() -> None:
    writer, _ = _writer()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    queue.put_nowait(_EOF)
    assert await _await_connect(queue, writer) is None


async def test_command_loop_disconnects_on_eof() -> None:
    writer, _ = _writer()
    stdin_queue: asyncio.Queue[Any] = asyncio.Queue()
    command_queue: asyncio.Queue[Any] = asyncio.Queue()
    stdin_queue.put_nowait(_EOF)

    await _command_loop(stdin_queue, command_queue, writer)
    command = command_queue.get_nowait()
    assert command == Disconnect()


def test_parse_command_boolean_string_coercion() -> None:
    assert parse_command({"type": "set_mic_gate", "open": "false"}) == SetMicGate(open=False)
    assert parse_command({"type": "set_mic_gate", "open": "0"}) == SetMicGate(open=False)
    assert parse_command({"type": "set_mic_gate", "open": "off"}) == SetMicGate(open=False)
    assert parse_command({"type": "set_mic_gate", "open": "true"}) == SetMicGate(open=True)
    assert parse_command(
        {"type": "set_share_screen", "enabled": "false"}
    ) == SetShareScreen(enabled=False)
    assert parse_command(
        {"type": "set_share_screen", "enabled": "0"}
    ) == SetShareScreen(enabled=False)


def test_parse_command_unhashable_mode_rejected_with_value_error() -> None:
    with pytest.raises(ValueError):
        parse_command({"type": "set_gating_mode", "mode": {"nested": "dict"}})
    with pytest.raises(ValueError):
        parse_command({"type": "set_gating_mode", "mode": ["list"]})

