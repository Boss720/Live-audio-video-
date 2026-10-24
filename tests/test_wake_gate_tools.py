"""Regression tests for wake-gated tool calls (M3 defects B and C).

B) Post-tool continuations of an addressed turn must stay addressed, and any
   tool call dropped by the gate must still get a FunctionResponse so the
   model does not wait forever.
C) A voice tool call that arrives before any input transcription must wait
   briefly for ASR instead of dispatching ungated.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from audit.journal import Journal
from core.commands import Disconnect, SendText
from core.events import ConnectionState, ConnectionStateChanged, SessionError
from core.session import LiveSession
from persist.store import Store
from settings.settings import AppSettings
from tests.fakes.fake_live import FakeLive, FakeLiveSession, make_fake_client_factory


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, str, dict[str, Any]]] = []

    async def submit(self, call_id: str, name: str, args: dict[str, Any]) -> Any:
        self.submissions.append((call_id, name, args))

    def spawn(self, coro: Any) -> None:
        asyncio.create_task(coro)

    def cancel(self, call_ids: list[str]) -> None:
        pass


class _FakeSpeaker:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.flushes = 0
        self.level = 0.0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        self.flushes += 1


def _make_settings(tmp_path: Path, **overrides: Any) -> AppSettings:
    defaults: dict[str, Any] = {
        "model": "gemini-3.1-flash-live-preview",
        "voice": "Puck",
        "mode": "both",
        "working_dir": tmp_path,
        "workspace_root": tmp_path,
        "wake_word_enabled": True,
        "wake_word": "Bongo",
        "share_screen": False,
        "computer_use_enabled": False,
    }
    defaults.update(overrides)
    return AppSettings(**defaults)


class _Harness:
    def __init__(self, monkeypatch: Any, tmp_path: Path, **overrides: Any) -> None:
        self.live = FakeLive()
        monkeypatch.setattr("google.genai.Client", make_fake_client_factory(self.live))
        monkeypatch.setattr("settings.secrets.get_api_key", lambda: "fake-api-key")
        monkeypatch.setattr("media.devices.list_input_devices", lambda: [])
        monkeypatch.setattr("media.devices.list_output_devices", lambda: [])
        monkeypatch.setattr("media.screen.capture_jpeg", lambda *a, **k: b"jpeg")
        monkeypatch.setattr("core.session.capture_jpeg", lambda *a, **k: b"jpeg")
        self.command_queue: asyncio.Queue[Any] = asyncio.Queue()
        self.event_queue: asyncio.Queue[Any] = asyncio.Queue()
        self.speaker = _FakeSpeaker()
        self.session = LiveSession(
            settings=_make_settings(tmp_path, **overrides),
            command_queue=self.command_queue,
            event_queue=self.event_queue,
            journal=Journal(run_id="wake-gate-tools", log_dir=tmp_path),
            store=Store(db_path=tmp_path / "wake_gate_tools.db"),
            run_id="wake-gate-tools",
            speaker=self.speaker,  # type: ignore[arg-type]
        )
        self.dispatcher = _RecordingDispatcher()
        self.session._dispatcher = self.dispatcher  # type: ignore[assignment]
        self.task: asyncio.Task[Any] | None = None
        self.events: list[Any] = []

    async def start(self) -> FakeLiveSession:
        self.task = asyncio.create_task(self.session.run())
        while True:
            event = await asyncio.wait_for(self.event_queue.get(), timeout=5)
            self.events.append(event)
            if (
                isinstance(event, ConnectionStateChanged)
                and event.state == ConnectionState.LIVE
            ):
                break
        fake = self.live.latest_session
        assert fake is not None
        return fake

    async def stop(self) -> None:
        assert self.task is not None
        try:
            self.command_queue.put_nowait(Disconnect())
            await asyncio.wait_for(self.task, timeout=5)
        finally:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.session._journal._close()
        while not self.event_queue.empty():
            self.events.append(self.event_queue.get_nowait())

    def submitted_ids(self) -> list[str]:
        return [call_id for call_id, _, _ in self.dispatcher.submissions]

    def gate_errors(self) -> list[SessionError]:
        return [
            e
            for e in self.events
            if isinstance(e, SessionError) and "wake-word gate" in e.message
        ]


def _tool_responses(fake: FakeLiveSession) -> list[types.FunctionResponse]:
    responses: list[types.FunctionResponse] = []
    for item in fake.pop_outbox():
        if isinstance(item, types.LiveClientToolResponse):
            responses.extend(item.function_responses or [])
    return responses


def _transcription(text: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text=text)
        )
    )


def _model_audio(data: bytes = b"\x01\x02") -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                parts=[types.Part(inline_data=types.Blob(data=data, mime_type="audio/pcm"))]
            )
        )
    )


def _turn_complete() -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(turn_complete=True)
    )


@pytest.mark.asyncio
async def test_post_tool_continuation_is_addressed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Audio and chained tool calls after an addressed tool turn are not gated."""
    h = _Harness(monkeypatch, tmp_path)
    try:
        fake = await h.start()
        fake.emit(_transcription("Hey Bongo, read my notes"))
        fake.request_tool("call-1", "read_file", {"path": "notes.txt"})
        fake.emit(_turn_complete())
        # Continuation turn after the tool response: empty utterance.
        fake.emit(_model_audio())
        fake.request_tool("call-2", "read_file", {"path": "more.txt"})
        await asyncio.sleep(0.15)
    finally:
        await h.stop()

    assert h.submitted_ids() == ["call-1", "call-2"]
    assert h.speaker.writes == [b"\x01\x02"]
    assert h.speaker.flushes == 0
    assert h.gate_errors() == []


@pytest.mark.asyncio
async def test_continuation_after_tool_response_sent_is_addressed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Sending the tool response re-arms the continuation as addressed."""
    h = _Harness(monkeypatch, tmp_path)
    from tools.registry import ToolResult

    try:
        fake = await h.start()
        fake.emit(_transcription("Bongo, list files"))
        fake.request_tool("call-1", "list_dir", {"path": "."})
        fake.emit(_turn_complete())
        await asyncio.sleep(0.05)
        await h.session.send_tool_response(
            "call-1", "list_dir", ToolResult(ok=True, message="a.txt")
        )
        fake.emit(_model_audio())
        fake.request_tool("call-2", "read_file", {"path": "a.txt"})
        await asyncio.sleep(0.15)
    finally:
        await h.stop()

    assert h.submitted_ids() == ["call-1", "call-2"]
    assert h.speaker.flushes == 0


@pytest.mark.asyncio
async def test_gated_tool_call_gets_function_response(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A tool call dropped by the gate is answered so the model does not hang."""
    h = _Harness(monkeypatch, tmp_path)
    try:
        fake = await h.start()
        fake.emit(_transcription("did you see the game last night"))
        fake.request_tool("call-1", "read_file", {"path": "notes.txt"})
        await asyncio.sleep(0.15)
        responses = _tool_responses(fake)
    finally:
        await h.stop()

    assert h.submitted_ids() == []
    assert [r.id for r in responses] == ["call-1"]
    assert responses[0].name == "read_file"
    assert "error" in (responses[0].response or {})
    assert len(h.gate_errors()) == 1


@pytest.mark.asyncio
async def test_tool_call_before_transcription_without_wake_word(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """No transcription within the window -> unaddressed: not executed, answered."""
    monkeypatch.setattr("core.session.WAKE_TRANSCRIPTION_WAIT_SECONDS", 0.1)
    h = _Harness(monkeypatch, tmp_path)
    try:
        fake = await h.start()
        fake.request_tool("call-1", "read_file", {"path": "notes.txt"})
        await asyncio.sleep(0.02)
        # Still deciding: neither dispatched nor answered yet.
        assert h.submitted_ids() == []
        assert _tool_responses(fake) == []
        await asyncio.sleep(0.25)
        responses = _tool_responses(fake)
    finally:
        await h.stop()

    assert h.submitted_ids() == []
    assert [r.id for r in responses] == ["call-1"]
    assert "error" in (responses[0].response or {})


@pytest.mark.asyncio
async def test_tool_call_before_transcription_with_unaddressed_speech(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Transcription without the wake word inside the window -> still gated."""
    monkeypatch.setattr("core.session.WAKE_TRANSCRIPTION_WAIT_SECONDS", 0.1)
    h = _Harness(monkeypatch, tmp_path)
    try:
        fake = await h.start()
        fake.request_tool("call-1", "read_file", {"path": "notes.txt"})
        fake.emit(_transcription("just chatting with my colleague"))
        await asyncio.sleep(0.3)
        responses = _tool_responses(fake)
    finally:
        await h.stop()

    assert h.submitted_ids() == []
    assert [r.id for r in responses] == ["call-1"]


@pytest.mark.asyncio
async def test_tool_call_before_transcription_with_wake_word_in_window(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Wake word transcribed inside the window -> dispatched without waiting it out."""
    monkeypatch.setattr("core.session.WAKE_TRANSCRIPTION_WAIT_SECONDS", 2.0)
    h = _Harness(monkeypatch, tmp_path)
    try:
        fake = await h.start()
        fake.request_tool("call-1", "read_file", {"path": "notes.txt"})
        fake.emit(_transcription("Hey "))
        fake.emit(_transcription("Bongo, read my notes"))
        await asyncio.sleep(0.2)
        responses = _tool_responses(fake)
    finally:
        await h.stop()

    assert h.submitted_ids() == ["call-1"]
    assert responses == []
    assert h.gate_errors() == []


@pytest.mark.asyncio
async def test_typed_turn_tool_call_is_exempt(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Typed input is always addressed: no wait, immediate dispatch."""
    monkeypatch.setattr("core.session.WAKE_TRANSCRIPTION_WAIT_SECONDS", 5.0)
    h = _Harness(monkeypatch, tmp_path)
    try:
        fake = await h.start()
        h.command_queue.put_nowait(SendText(text="read my notes"))
        for _ in range(50):
            if h.session._text_turn_pending:
                break
            await asyncio.sleep(0.01)
        fake.request_tool("call-1", "read_file", {"path": "notes.txt"})
        await asyncio.sleep(0.15)
        responses = _tool_responses(fake)
    finally:
        await h.stop()

    assert h.submitted_ids() == ["call-1"]
    assert responses == []


@pytest.mark.asyncio
async def test_continuation_after_ignored_call_stays_gated(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The model's reaction to an ignored call is gated without another wait."""
    monkeypatch.setattr("core.session.WAKE_TRANSCRIPTION_WAIT_SECONDS", 5.0)
    h = _Harness(monkeypatch, tmp_path)
    try:
        fake = await h.start()
        fake.emit(_transcription("talking to someone else"))
        fake.request_tool("call-1", "read_file", {"path": "notes.txt"})
        fake.emit(_turn_complete())
        fake.emit(_model_audio())
        fake.request_tool("call-2", "read_file", {"path": "notes.txt"})
        await asyncio.sleep(0.15)
        responses = _tool_responses(fake)
    finally:
        await h.stop()

    assert h.submitted_ids() == []
    assert [r.id for r in responses] == ["call-1", "call-2"]
    assert h.speaker.writes == []


@pytest.mark.asyncio
async def test_text_mode_tool_call_is_not_held(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Text-only sessions send no audio, so nothing waits for ASR."""
    monkeypatch.setattr("core.session.WAKE_TRANSCRIPTION_WAIT_SECONDS", 5.0)
    h = _Harness(monkeypatch, tmp_path, mode="text")
    try:
        fake = await h.start()
        fake.request_tool("call-1", "read_file", {"path": "notes.txt"})
        await asyncio.sleep(0.15)
        responses = _tool_responses(fake)
    finally:
        await h.stop()

    assert h.submitted_ids() == ["call-1"]
    assert responses == []
