"""Single-epoch WebSocket driver for the Gemini Live API.

A ``LiveSession`` owns one Google Live API connection, the media send/receive
loops, and (optionally) a command dispatcher for that single epoch. It is
intentionally a single-socket object; resumption and reconnect logic live in
``core.supervisor.Supervisor``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import google.genai as genai
import google.genai.errors
from google.genai import types

from audit.journal import Journal
from core.commands import (
    AgentCommand,
    ApproveTool,
    CancelTool,
    DenyTool,
    Disconnect,
    Interrupt,
    SendText,
    SetGatingMode,
    SetMicGate,
    SetShareScreen,
)
from core.dispatcher import ToolDispatcher
from core.events import (
    API_CLOSE,
    USER_REQUEST,
    AgentEvent,
    AudioLevel,
    ConnectionState,
    ConnectionStateChanged,
    DiagnosticsUpdate,
    DisconnectReason,
    PartialTranscript,
    RateLimitHit,
    SessionError,
    SessionExpiring,
    TextNotSent,
    TurnComplete,
    UsageUpdate,
)
from core.turn_state import TurnState
from core.wake_word import contains_wake_word
from media import devices
from media.microphone import INPUT_SAMPLE_RATE, Microphone
from media.screen import (
    DEFAULT_MAX_BYTES,
    ScreenCapture,
    capture_jpeg,
    validate_monitor,
)
from media.speaker import Speaker
from obs.logging_setup import get_logger
from obs.metrics import RuntimeMetrics
from persist.store import Store
from policy import PolicyEngine
from settings.secrets import get_api_key
from settings.settings import AppSettings, canonicalize_model_name
from tools import build_function_declarations
from tools import computer as computer_tools
from tools import registry as tool_registry
from tools._state import set_policy_engine, set_workspace_root
from tools.registry import COMPUTER_TOOL_NAMES, ToolResult

logger = get_logger(__name__)

_MAX_VIDEO_SEND_ERRORS = 5
_METER_INTERVAL_SECONDS = 0.033
# Typed text waits at most this long for the pre-send screen frame; Disconnect
# and approval commands queue behind the send loop, so it must not stall.
_FRAME_WAIT_SECONDS = 1.5
# Event-queue noise caps: diagnostics snapshots at 1 Hz, audio meters at 15 Hz.
_DIAGNOSTICS_MIN_INTERVAL_SECONDS = 1.0
_METER_EMIT_MIN_INTERVAL_SECONDS = 1 / 15
# Time given to the desktop to repaint after a computer-use action before the
# post-action frame is grabbed.
_POST_ACTION_SETTLE_SECONDS = 0.15
# How long a voice tool call that arrives before any input transcription is
# held while ASR catches up, before it is treated as unaddressed. Public so
# tests can patch it.
WAKE_TRANSCRIPTION_WAIT_SECONDS = 0.3
# FunctionResponse body for tool calls dropped by the wake-word gate. The model
# must always get an answer, or it waits on the call forever.
_GATED_TOOL_RESPONSE: dict[str, Any] = {
    "error": "ignored: user did not address the assistant"
}

# Per-1M-token pricing (input USD, output USD) for the Live models, keyed by
# model-name prefix. The session does not know which pricing tier it is
# running on (the supervisor does), so it always computes the paid-tier
# estimate; free-tier sessions should surface ``estimated_usd=None`` on the
# ``UsageUpdate`` event so the GUI can show "Free" instead of a dollar figure.
_COST_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-live-preview": (0.15, 0.60),
    "gemini-2.5-flash-native-audio": (0.15, 0.60),
    "gemini-3.8-live": (0.75, 3.75),
}
_DEFAULT_COST_PER_MILLION_TOKENS = (0.15, 0.60)


def _modality_name(modality: Any) -> str:
    """Return the bare modality name, e.g. ``"VIDEO"``.

    ``usage_metadata`` reports modalities as ``MediaModality`` enum members
    whose ``str()`` is ``"MediaModality.VIDEO"`` — comparing that against
    ``"VIDEO"`` never matches, which silently zeroed every per-modality token
    count. Read ``name``/``value`` first and fall back to the trailing segment
    so a plain string still works.
    """
    name = getattr(modality, "name", None) or getattr(modality, "value", None)
    if name is None:
        name = str(modality).rsplit(".", 1)[-1]
    return str(name).upper()


def _cost_rates_for_model(model: str) -> tuple[float, float]:
    """Return the (input, output) per-1M-token USD rates for ``model``."""
    for prefix, rates in _COST_PER_MILLION_TOKENS.items():
        if model.startswith(prefix):
            return rates
    return _DEFAULT_COST_PER_MILLION_TOKENS


# ---------------------------------------------------------------------------
# Module 3: non-blocking tools & session-level API features
# ---------------------------------------------------------------------------

# ``tools.registry.FUNCTION_SCHEDULES`` value -> ``FunctionDeclaration``
# behavior. The Live API has no session-level scheduling field on
# ``LiveConnectConfig``; non-blocking scheduling is expressed per declaration
# via ``behavior`` (BidiGenerateContent-only).
_BEHAVIOR_BY_SCHEDULE: dict[str, types.Behavior] = {
    "non_blocking_when_idle": types.Behavior.NON_BLOCKING,
    "non_blocking_silent": types.Behavior.NON_BLOCKING,
    "blocking_interrupt": types.Behavior.BLOCKING,
}

# How a tool *response* is scheduled back into the conversation. Only set for
# NON_BLOCKING declarations; the API ignores ``scheduling`` on blocking calls.
_RESPONSE_SCHEDULING_BY_SCHEDULE: dict[str, types.FunctionResponseScheduling] = {
    "non_blocking_when_idle": types.FunctionResponseScheduling.WHEN_IDLE,
    "non_blocking_silent": types.FunctionResponseScheduling.SILENT,
}

# Extended Thinking models keep reasoning after a spoken filler and report
# "idle" late, so WHEN_IDLE would park click/screenshot/shell results behind the
# model. Mutating/computer tools (blocking_interrupt) must land immediately;
# read-only tools stay WHEN_IDLE.
_EXTENDED_RESPONSE_SCHEDULING_BY_SCHEDULE: dict[str, types.FunctionResponseScheduling] = {
    **_RESPONSE_SCHEDULING_BY_SCHEDULE,
    "blocking_interrupt": types.FunctionResponseScheduling.INTERRUPT,
}

_MEDIA_RESOLUTION_BY_SETTING: dict[str, types.MediaResolution] = {
    "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
    "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
}

_THINKING_LEVEL_BY_SETTING: dict[str, types.ThinkingLevel] = {
    "minimal": types.ThinkingLevel.MINIMAL,
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}


def _model_id(model: str) -> str:
    """Return the model id without an optional ``models/`` resource prefix."""
    return canonicalize_model_name(model).rsplit("/", 1)[-1]


def _is_extended_thinking_model(model: str) -> bool:
    return _model_id(model).startswith("gemini-3.8-live-extended-thinking")


def _is_gemini_38_live_model(model: str) -> bool:
    return _model_id(model).startswith("gemini-3.8-live")


def _supports_google_search(model: str) -> bool:
    """Whether the model accepts the Google Search grounding tool.

    Verified live against the API: ``gemini-3.1-flash-live-preview``,
    ``gemini-3.8-live``, and ``gemini-3.8-live-extended-thinking`` reject
    google_search at connect time (1011 quota/internal errors); the 2.5
    native-audio family accepts it.
    """
    return _model_id(model).startswith("gemini-2.5-flash-native-audio")


def _supports_affective_dialog(model: str) -> bool:
    """Whether the model accepts affective dialog.

    Verified live against the API: ``gemini-3.1-flash-live-preview`` rejects
    affective dialog at connect time (1011 quota/internal errors); the 2.5
    native-audio family accepts it.
    """
    return _model_id(model).startswith("gemini-2.5-flash-native-audio")


def _build_tools(settings: AppSettings) -> list[types.Tool]:
    """Build the tool list, preferring the registry's consolidated builder.

    ``build_tools`` (single consolidated ``Tool`` plus conditional
    google_search / code_execution tools) supersedes the per-declaration
    ``build_function_declarations`` entry point; fall back to the legacy path
    when the registry does not expose it yet. google_search is dropped on
    models that reject it.
    """
    build_tools = getattr(tool_registry, "build_tools", None)
    if build_tools is not None:
        tools = list(build_tools(settings))
    else:
        tools = list(
            build_function_declarations(computer_use=settings.computer_use_enabled)
        )
    if not _supports_google_search(settings.model):
        tools = [tool for tool in tools if tool.google_search is None]
    return tools


def _tool_schedules() -> dict[str, str]:
    """Return the registry's ``FUNCTION_SCHEDULES`` (empty if unavailable)."""
    schedules = getattr(tool_registry, "FUNCTION_SCHEDULES", None)
    return dict(schedules) if schedules else {}


def _apply_function_behavior(
    tools: list[types.Tool], model: str | None = None
) -> list[types.Tool]:
    """Stamp each declaration with the ``Behavior`` its schedule implies."""
    if model is not None and _is_extended_thinking_model(model):
        return [
            tool.model_copy(
                update={
                    "function_declarations": [
                        declaration.model_copy(
                            update={"behavior": types.Behavior.NON_BLOCKING}
                        )
                        for declaration in (tool.function_declarations or [])
                    ]
                }
            )
            if tool.function_declarations
            else tool
            for tool in tools
        ]

    schedules = _tool_schedules()
    if not schedules:
        return tools
    result: list[types.Tool] = []
    for tool in tools:
        declarations = tool.function_declarations
        if not declarations:
            result.append(tool)
            continue
        updated = [
            declaration.model_copy(
                update={"behavior": _BEHAVIOR_BY_SCHEDULE[schedules[declaration.name]]}
            )
            if declaration.name in schedules
            and schedules[declaration.name] in _BEHAVIOR_BY_SCHEDULE
            else declaration
            for declaration in declarations
        ]
        result.append(tool.model_copy(update={"function_declarations": updated}))
    return result


def _response_scheduling_for(
    name: str, model: str | None = None
) -> types.FunctionResponseScheduling | None:
    """Scheduling to attach to a tool response, or ``None`` for blocking tools."""
    schedule = _tool_schedules().get(name)
    if model is not None and _is_extended_thinking_model(model):
        return _EXTENDED_RESPONSE_SCHEDULING_BY_SCHEDULE.get(
            schedule or "", types.FunctionResponseScheduling.WHEN_IDLE
        )
    if schedule is None:
        return None
    return _RESPONSE_SCHEDULING_BY_SCHEDULE.get(schedule)


def _session_config_fields(settings: AppSettings) -> dict[str, Any]:
    """Module-3 ``LiveConnectConfig`` fields layered over ``as_connect_config``.

    Note: ``safety_settings`` exists on the SDK's ``LiveConnectConfig`` but the
    Live API backend rejects it (``Unknown name "safetySettings" at 'setup'``),
    so it is deliberately not sent.
    """
    fields: dict[str, Any] = {
        "media_resolution": _MEDIA_RESOLUTION_BY_SETTING[settings.media_resolution],
        "enable_affective_dialog": settings.enable_affective_dialog
        and _supports_affective_dialog(settings.model),
    }
    # Gemini 3.8 Live has a fixed latency profile and rejects thinking_config.
    # Its Extended Thinking sibling accepts low/medium/high and needs the
    # interaction_status lifecycle handled by the Live SDK.
    if not _is_gemini_38_live_model(settings.model) or _is_extended_thinking_model(
        settings.model
    ):
        fields["thinking_config"] = types.ThinkingConfig(
            thinking_level=_THINKING_LEVEL_BY_SETTING[settings.thinking_level]
        )
    return fields


def _interaction_in_progress(server_content: Any) -> bool:
    """Whether a Live server message says reasoning continues after this turn."""
    status = getattr(server_content, "interaction_status", None)
    status = getattr(status, "value", status)
    status_name = str(status).rsplit(".", 1)[-1].upper()
    return status_name in {"IN_PROGRESS", "REQUIRES_ACTION"}


def _should_reset_turn_gate(model: str, server_content: Any) -> bool:
    """Keep wake-word/frame state across Extended Thinking filler turns."""
    if getattr(server_content, "interrupted", False):
        return True
    if not getattr(server_content, "turn_complete", False):
        return False
    return not (
        _is_extended_thinking_model(model)
        and _interaction_in_progress(server_content)
    )


def _turn_state_content(
    model: str, server_content: types.LiveServerContent
) -> types.LiveServerContent:
    """Keep Extended Thinking filler turns inside the same transcript turn."""
    if _is_extended_thinking_model(model) and _interaction_in_progress(server_content):
        return server_content.model_copy(update={"turn_complete": False})
    return server_content


class LiveSession:
    """Single-session manager for a Gemini Live connection.

    Parameters
    ----------
    settings:
        Runtime settings used to build the connect config and media objects.
    command_queue:
        Queue of commands from the GUI/driver.
    event_queue:
        Queue of events published to the GUI/driver.
    journal:
        Audit journal for tool attempts and results.
    store:
        Persistent store for run state (used by later phases).
    run_id:
        Identifier for this run.
    epoch:
        Resumption epoch (always ``0`` in Phase 2).
    resumption_handle:
        Optional handle to request session resumption from the API.
    mic:
        Optional injected microphone. If provided, the session will not start or
        stop it.
    speaker:
        Optional injected speaker. If provided, the session will not start or stop
        it.
    screen:
        Optional injected screen capture. If provided, the session will not start or
        stop it.
    dispatcher:
        Optional injected tool dispatcher. If provided, the session will not create
        one and will not set its respond callback.
    turn_state:
        Optional injected turn state. If provided, the session will not create one.
    on_resumption_handle:
        Optional callback invoked when the server updates resumption availability.
    on_connected:
        Optional callback invoked after the session connects successfully.
    respond_callback:
        Optional callback used to send tool responses when a dispatcher is created
        by the session.
    """

    def __init__(
        self,
        settings: AppSettings,
        command_queue: asyncio.Queue[AgentCommand],
        event_queue: asyncio.Queue[Any],
        journal: Journal,
        store: Store,
        run_id: str,
        epoch: int = 0,
        *,
        resumption_handle: str | None = None,
        mic: Microphone | None = None,
        speaker: Speaker | None = None,
        screen: ScreenCapture | None = None,
        dispatcher: ToolDispatcher | None = None,
        turn_state: TurnState | None = None,
        on_resumption_handle: Callable[[str, bool], Awaitable[None]] | None = None,
        on_connected: Callable[[LiveSession], Awaitable[None]] | None = None,
        respond_callback: Callable[[str, str, ToolResult], Awaitable[bool]] | None = None,
        free_tier: bool = False,
    ) -> None:
        self._settings = settings
        self._free_tier = free_tier
        self._command_queue = command_queue
        self._event_queue = event_queue
        self._journal = journal
        self._store = store
        self._run_id = run_id
        self._epoch = epoch
        self._resumption_handle = resumption_handle

        self._turn_state = turn_state if turn_state is not None else TurnState()
        self._dispatcher = dispatcher

        self._mic = mic
        self._speaker = speaker
        self._screen = screen
        self._screen_monitor = settings.screen_monitor

        self._on_resumption_handle = on_resumption_handle
        self._on_connected = on_connected
        self._respond_callback = respond_callback

        self._owns_mic = mic is None
        self._owns_speaker = speaker is None
        self._owns_screen = screen is None
        self._owns_dispatcher = dispatcher is None
        self._owns_turn_state = turn_state is None

        self._session: Any | None = None
        self._latest_resumption_handle: str | None = None
        self._closed = False
        self._rate_limit_emitted = False

        # Whether audio has streamed since the last ``audio_stream_end``
        # marker; used to send the marker exactly when it adds information.
        self._audio_streamed = False
        # Cumulative token counters for the session cost estimate.
        self._prompt_tokens_used = 0
        self._completion_tokens_used = 0

        self._video_send_task: asyncio.Task[Any] | None = None
        # On-demand frame state: when the last frame went out, and a digest of
        # it so an unchanged screen is not billed again.
        self._last_frame_sent = 0.0
        self._last_frame_digest: bytes | None = None
        # One frame per user utterance, so "what am I looking at?" does not wait
        # for the next heartbeat. Strong refs: asyncio only holds weak ones.
        self._frame_tasks: set[asyncio.Task[Any]] = set()
        self._frame_lock = asyncio.Lock()
        self._user_frame_task: asyncio.Task[bool] | None = None
        self._frame_utterance = 0
        self._frame_sent_this_utterance = False
        self._shutdown_event = asyncio.Event()
        self._mic_gate_open = True

        # Wake-word gating state. ``_utterance_text`` accumulates the current
        # voice utterance's input transcription; when the model starts a turn
        # we decide once whether that utterance addressed us. Typed SendText
        # turns are always in scope and exempt via ``_text_turn_pending``.
        self._utterance_text = ""
        self._text_turn_pending = False
        self._turn_evaluated = False
        self._turn_gated = False
        # User pressed Interrupt mid-reply: drop the rest of this model turn's
        # audio. Cleared at the turn boundary or a server-side interruption.
        self._user_interrupted = False
        # Any model output (audio or transcript) seen since the last boundary.
        self._model_turn_active = False
        # Post-tool continuation: the model turn that follows a tool response
        # carries no new user speech, so it inherits the decision of the turn
        # that issued the call ("addressed" / "gated") until the next input
        # transcription arrives. Survives ``_reset_wake_gate``.
        self._continuation: str | None = None
        self._addressed_call_ids: set[str] = set()
        # Voice tool calls held while waiting for ASR (see
        # ``WAKE_TRANSCRIPTION_WAIT_SECONDS``). While calls are held, a turn
        # boundary defers the gate reset until the decision is made.
        self._pending_gate_calls: list[Any] = []
        self._gate_wait_task: asyncio.Task[None] | None = None
        self._gate_input_changed = asyncio.Event()
        self._reset_after_gate_decision = False
        self._turn_events: asyncio.Queue[tuple[AgentEvent, float] | None] = asyncio.Queue()
        self._undecided_turn_events: list[AgentEvent] = []
        self._metrics = RuntimeMetrics()
        self._last_video_sent_at: float | None = None
        self._last_diagnostics_at: float | None = None
        self._last_meter: dict[str, tuple[float, float]] = {}
        # Speech end -> first model audio (per turn). ``_speech_end_at`` is the
        # earliest reliable end-of-user-speech signal the session has: the
        # client ``audio_stream_end`` on mute, else the latest server input
        # transcription chunk (server VAD sends no explicit activity end). It
        # is consumed by the first audio chunk of the turn, so interruptions
        # and late transcription chunks cannot double count.
        self._speech_end_at: float | None = None
        self._first_audio_seen = False

    async def run(self) -> DisconnectReason:
        """Connect to the API, spawn loops, and return when the session ends."""
        try:
            # An injected dispatcher belongs to the supervisor, which already
            # pointed the tool-level policy at that dispatcher's engine and
            # workspace. Replacing it here would split approvals and tool
            # checks across two engines with different deny globs.
            policy: PolicyEngine | None = None
            if self._owns_dispatcher:
                policy = self._build_policy()
                set_policy_engine(policy)
                set_workspace_root(self._settings.workspace_root)
            computer_tools.set_monitor(self._settings.screen_monitor)

            api_key = get_api_key()
            if api_key is None:
                raise ValueError("No API key configured")

            client = genai.Client(api_key=api_key)
            canonical_model = canonicalize_model_name(self._settings.model)
            connect_config = self._settings.as_connect_config(
                tools=_apply_function_behavior(
                    _build_tools(self._settings), canonical_model
                ),
                resumption_handle=self._resumption_handle,
            )
            connect_config = connect_config.model_copy(
                update=_session_config_fields(self._settings)
            )

            await self._emit(ConnectionStateChanged(ConnectionState.CONNECTING))
            async with client.aio.live.connect(
                model=canonical_model,
                config=connect_config,
            ) as session:
                self._session = session
                await self._emit(ConnectionStateChanged(ConnectionState.LIVE))
                self._journal.connection_event(
                    "connected", canonical_model, epoch=self._epoch
                )
                if self._on_connected is not None:
                    await self._on_connected(self)

                reason = await self._run_session(session, policy)
                await self._emit(reason)
                return reason

        except Exception as exc:
            logger.exception("Session failed")
            reason = self._classify_disconnect(exc)
            await self._maybe_emit_rate_limit(exc)
            await self._emit(
                ConnectionStateChanged(
                    ConnectionState.ERROR,
                    detail=reason.detail,
                )
            )
            await self._emit(reason)
            return reason
        finally:
            await self._emit(ConnectionStateChanged(ConnectionState.DISCONNECTED))
            self._journal.connection_event("disconnected", "", epoch=self._epoch)
            self._session = None
            self._closed = True

    def _build_policy(self) -> PolicyEngine:
        """Policy for a session that owns its dispatcher."""
        return PolicyEngine(
            allow_roots=[self._settings.workspace_root],
            deny_globs=[r"*\.git\*", "__pycache__"],
            never_allow_regex=[
                r"AIza[0-9A-Za-z\-_]{35}",
                r"sk-[A-Za-z0-9]{20,}",
                r"ghp_[A-Za-z0-9]{36}",
            ],
        )

    async def _run_session(
        self, session: Any, policy: PolicyEngine | None
    ) -> DisconnectReason:
        """Start media and the concurrent send/receive loops."""
        if self._dispatcher is None:
            if policy is None:
                policy = self._build_policy()
            self._dispatcher = ToolDispatcher(
                policy=policy,
                journal=self._journal,
                event_queue=self._event_queue,
                run_id=self._run_id,
                epoch=self._epoch,
                store=self._store,
            )
            respond_callback = self._respond_callback
            if respond_callback is None:
                respond_callback = self._make_respond_callback(session)
            self._dispatcher.set_respond_callback(respond_callback)

        tasks: set[asyncio.Task[Any]] = set()
        self._turn_state.set_timer_callback(self._queue_turn_events)
        transcript_task = asyncio.create_task(
            self._deliver_turn_events(), name="transcript_delivery"
        )
        tasks.add(transcript_task)
        try:
            if self._audio_output_available() and self._speaker is None:
                self._speaker = Speaker(device=self._settings.output_device)
                await self._speaker.start()

            if (
                self._settings.mode != "text"
                and self._audio_input_available()
                and self._mic is None
            ):
                mic_extra: dict[str, Any] = {}
                speaker = self._speaker
                if speaker is not None and devices.should_gate_echo(
                    self._settings.echo_gate, self._settings.output_device
                ):
                    mic_extra["echo_gate"] = lambda: speaker.is_playing
                self._mic = Microphone(
                    device=self._settings.input_device,
                    chunk_duration_ms=self._settings.audio_chunk_duration_ms,
                    **mic_extra,
                )
                await self._mic.start()

            wants_screen = (
                self._settings.share_screen or self._settings.computer_use_enabled
            )
            # On-demand mode grabs its own frames through ``capture_jpeg``, so
            # the continuous capture thread is only started for "stream" mode.
            if (
                wants_screen
                and self._settings.frame_mode == "stream"
                and self._screen is None
            ):
                self._screen = ScreenCapture(
                    fps=self._settings.screen_fps,
                    monitor=self._settings.screen_monitor,
                    computer_use=self._settings.computer_use_enabled,
                    max_bytes=DEFAULT_MAX_BYTES,
                )
                await self._screen.start()

            tasks.add(
                asyncio.create_task(
                    self._receive_loop(session),
                    name="receive",
                )
            )
            tasks.add(asyncio.create_task(self._diagnostics_loop(), name="runtime_diagnostics"))
            tasks.add(
                asyncio.create_task(
                    self._send_loop(session),
                    name="send",
                )
            )

            if self._mic is not None:
                tasks.add(
                    asyncio.create_task(
                        self._audio_send_loop(session, self._mic),
                        name="audio_send",
                    )
                )
            if self._screen is not None or (
                wants_screen and self._settings.frame_mode == "on_demand"
            ):
                self._video_send_task = asyncio.create_task(
                    self._video_send_loop(session, self._screen),
                    name="video_send",
                )
                tasks.add(self._video_send_task)
            if (
                self._mic is not None or self._speaker is not None
            ) and (self._owns_mic or self._owns_speaker):
                tasks.add(
                    asyncio.create_task(
                        self._meter_loop(),
                        name="meter",
                    )
                )

            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            reason: DisconnectReason | None = None
            for task in done:
                exc = task.exception()
                if exc is not None:
                    reason = self._classify_disconnect(exc)
                    break
                result = task.result()
                if isinstance(result, DisconnectReason):
                    reason = result

            if reason is None:
                reason = API_CLOSE

            # Signal remaining loops to exit, then cancel pending tasks.
            self._shutdown_event.set()
            await self._stop_media()

            for task in pending:
                if task is transcript_task:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            # Tell the API the audio stream ended before the WebSocket closes
            # so VAD can finalize any in-flight utterance. Best-effort: the
            # session may already be broken.
            await self._send_audio_stream_end(session)

            # Final snapshot so the last second of timings is not lost.
            await self._emit_diagnostics(force=True)

            # Flush any partial transcript that arrived just before close.
            self._turn_state.set_timer_callback(None)
            self._queue_turn_events(self._turn_state.flush())
            self._undecided_turn_events.clear()
            if not transcript_task.done():
                self._turn_events.put_nowait(None)
                await transcript_task

            return reason

        finally:
            self._turn_state.set_timer_callback(None)
            self._turn_state.flush()  # Cancel timers even when the session is cancelled.
            self._undecided_turn_events.clear()
            self._shutdown_event.set()
            if self._gate_wait_task is not None and not self._gate_wait_task.done():
                self._gate_wait_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._gate_wait_task
            self._gate_wait_task = None
            self._pending_gate_calls.clear()
            for t in list(self._frame_tasks):
                t.cancel()
            for t in tasks:
                if not t.done():
                    t.cancel()
            for t in tasks:
                if not t.done():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t
            await self._stop_media()

    def _queue_turn_events(self, events: list[AgentEvent]) -> None:
        """Resolve wake gating before queuing transcript events in arrival order."""
        if self._settings.wake_word_enabled and not self._turn_evaluated:
            for event in events:
                if isinstance(event, (PartialTranscript, TurnComplete)):
                    self._undecided_turn_events.append(event)
                else:
                    self._turn_events.put_nowait((event, self._metrics.stamp()))
            return
        events = self._undecided_turn_events + events
        self._undecided_turn_events.clear()
        for event in events:
            if self._turn_gated and isinstance(event, (PartialTranscript, TurnComplete)):
                continue
            self._turn_events.put_nowait((event, self._metrics.stamp()))

    async def _deliver_turn_events(self) -> None:
        """Serialize timer and packet events, including final-turn persistence."""
        while True:
            queued = await self._turn_events.get()
            try:
                if queued is None:
                    return
                event, queued_at = queued
                await self._emit(event)
                # This measures local publish delay, excluding coalescing/VAD.
                self._metrics.record_elapsed("transcript_queue_ms", queued_at)
                if isinstance(event, TurnComplete):
                    self._store.append_turn(
                        f"{self._run_id}-{self._epoch}-{event.source.value}-{event.turn_id}",
                        self._run_id,
                        event.source.value,
                        event.text,
                        datetime.now(UTC).isoformat(),
                        self._epoch,
                    )
            finally:
                self._turn_events.task_done()

    def _evaluate_wake_gate_if_needed(self) -> bool:
        """Evaluate wake-word addressing if not already evaluated for this turn.

        Returns True only on the call that first triggers the gate, so callers
        can flush playback and surface an activity event exactly once per gated turn.
        """
        if not self._settings.wake_word_enabled:
            return False
        if not self._turn_evaluated:
            self._turn_evaluated = True
            self._gate_input_changed.set()
            if self._text_turn_pending:
                basis = "typed"
            elif self._continuation is not None:
                basis = f"continuation:{self._continuation}"
            else:
                basis = "speech"
            addressed = (
                self._text_turn_pending
                or self._continuation == "addressed"
                or contains_wake_word(
                    self._utterance_text,
                    self._settings.wake_word,
                    self._settings.wake_word_aliases,
                )
            )
            logger.info(
                "[WAKE] %s (%s) wake=%r aliases=%r heard=%r",
                "ADDRESSED" if addressed else "GATED (no response sent)",
                basis,
                self._settings.wake_word,
                self._settings.wake_word_aliases,
                self._utterance_text.strip()[:200],
            )
            if not addressed:
                self._turn_gated = True
                self._queue_turn_events([])
                return True
            self._queue_turn_events([])
        return False

    def _update_wake_gate(self, server_content: Any) -> bool:
        """Update wake-word gating from a server content chunk.

        Returns True only on the chunk that first triggers the gate, so the
        caller can flush playback and surface an activity event exactly once
        per gated turn. No-op when wake-word gating is disabled.
        """
        if not self._settings.wake_word_enabled:
            return False

        input_transcription = server_content.input_transcription
        if input_transcription and input_transcription.text:
            self._utterance_text += input_transcription.text
            # New user speech ends any inherited post-tool decision.
            self._continuation = None
            self._gate_input_changed.set()

        has_model_output = bool(
            (server_content.model_turn and server_content.model_turn.parts)
            or (
                server_content.output_transcription
                and server_content.output_transcription.text
            )
        )
        if has_model_output:
            return self._evaluate_wake_gate_if_needed()
        return False

    def _reset_wake_gate(self) -> None:
        """Clear per-turn gating state at a turn boundary.

        ``_continuation`` is deliberately kept: it describes the *next* turn.
        """
        self._utterance_text = ""
        self._text_turn_pending = False
        self._turn_evaluated = False
        self._turn_gated = False
        self._reset_after_gate_decision = False

    def _on_turn_boundary_gate(self) -> None:
        """Reset gating at a turn boundary unless a held tool call is undecided."""
        if self._pending_gate_calls:
            # The turn is not over until its tool calls are answered; the
            # resolver resets once it has decided.
            self._reset_after_gate_decision = True
            return
        self._reset_wake_gate()

    def _needs_transcription_wait(self) -> bool:
        """Whether a voice tool call must wait for ASR before the gate decides."""
        return (
            self._settings.wake_word_enabled
            and not self._turn_evaluated
            and not self._text_turn_pending
            and self._continuation is None
            and not self._utterance_text.strip()
        )

    def _wake_word_heard(self) -> bool:
        return contains_wake_word(
            self._utterance_text,
            self._settings.wake_word,
            self._settings.wake_word_aliases,
        )

    async def _announce_gate_trigger(self, message: str) -> None:
        """Stop playback and surface a non-fatal notice for a newly gated turn."""
        if self._speaker is not None:
            self._speaker.flush()
        await self._emit(SessionError(message=message, fatal=False))

    async def _handle_tool_calls(self, session: Any, calls: list[Any]) -> None:
        """Gate, hold, or dispatch the function calls of one ``tool_call`` message."""
        if not self._settings.wake_word_enabled:
            self._dispatch_tool_calls(calls, track=False)
            return
        if self._settings.mode == "text" and not self._turn_evaluated:
            # Text-only sessions send no audio, so no call is voice-originated.
            self._dispatch_tool_calls(calls, track=True)
            return
        if self._pending_gate_calls:
            # A decision for this turn is already pending; join it.
            self._pending_gate_calls.extend(calls)
            return
        if self._needs_transcription_wait():
            self._pending_gate_calls.extend(calls)
            logger.info(
                "[WAKE] holding %d tool call(s) up to %.0f ms for input transcription",
                len(calls),
                WAKE_TRANSCRIPTION_WAIT_SECONDS * 1000,
            )
            self._gate_input_changed.clear()
            self._gate_wait_task = asyncio.create_task(
                self._await_transcription_then_resolve(session),
                name="wake_gate_wait",
            )
            return
        await self._decide_and_route(session, calls)

    async def _decide_and_route(self, session: Any, calls: list[Any]) -> None:
        """Evaluate the gate (once per turn) and dispatch or answer ``calls``."""
        if self._evaluate_wake_gate_if_needed():
            await self._announce_gate_trigger(
                "Ignored utterance: wake-word gate dropped turn "
                f"(wake={self._settings.wake_word!r})"
            )
        if self._turn_gated:
            self._continuation = "gated"
            await self._respond_gated_tool_calls(session, calls)
        else:
            self._dispatch_tool_calls(calls, track=True)

    async def _await_transcription_then_resolve(self, session: Any) -> None:
        """Hold the pending calls until the wake word is heard or time runs out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAKE_TRANSCRIPTION_WAIT_SECONDS
        while (
            self._pending_gate_calls
            and not self._turn_evaluated
            and not self._wake_word_heard()
        ):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            # No await between the checks above and this clear, so a set()
            # from the receive loop cannot be lost.
            self._gate_input_changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._gate_input_changed.wait(), remaining)
        calls = self._pending_gate_calls
        self._pending_gate_calls = []
        self._gate_wait_task = None
        if calls:
            await self._decide_and_route(session, calls)
        if self._reset_after_gate_decision:
            self._reset_wake_gate()

    def _dispatch_tool_calls(self, calls: list[Any], *, track: bool) -> None:
        """Hand addressed calls to the dispatcher; arm the addressed continuation."""
        if track:
            self._continuation = "addressed"
        for fc in calls:
            if track and fc.id:
                self._addressed_call_ids.add(fc.id)
            if self._dispatcher is not None:
                # Tracked by the dispatcher, not by this session: the call
                # must outlive a dropped socket so its result reaches
                # whichever session is connected when it finishes.
                self._dispatcher.spawn(
                    self._dispatcher.submit(fc.id, fc.name, fc.args or {})
                )

    def _note_tool_response_sent(self, call_id: str) -> None:
        """A response for an addressed call re-arms the addressed continuation."""
        if call_id in self._addressed_call_ids:
            self._addressed_call_ids.discard(call_id)
            self._continuation = "addressed"

    async def _respond_gated_tool_calls(self, session: Any, calls: list[Any]) -> None:
        """Answer calls dropped by the wake-word gate so the model never hangs."""
        if not calls:
            return
        logger.info(
            "[WAKE] answering %d gated tool call(s) as ignored: %s",
            len(calls),
            ", ".join(f"{fc.name}#{fc.id}" for fc in calls),
        )
        responses = [
            types.FunctionResponse(
                id=fc.id,
                name=fc.name,
                response=dict(_GATED_TOOL_RESPONSE),
                scheduling=_response_scheduling_for(fc.name, self._settings.model),
            )
            for fc in calls
        ]
        try:
            await session.send_tool_response(function_responses=responses)
        except Exception as exc:
            logger.warning("Failed to answer gated tool call(s): %s", exc)

    async def _receive_loop(self, session: Any) -> DisconnectReason | None:
        """Consume server responses and dispatch side effects.

        ``session.receive()`` returns at the end of each model turn, so the
        outer ``while`` re-enters it for the next turn. This matches the
        Gemini Live SDK behavior and the original ``agent.py`` loop.
        """
        try:
            while not self._shutdown_event.is_set():
                async for response in session.receive():
                    received_at = self._metrics.stamp()
                    if self._shutdown_event.is_set():
                        break

                    resumption_update = response.session_resumption_update
                    if resumption_update is not None:
                        if resumption_update.resumable:
                            self._latest_resumption_handle = resumption_update.new_handle
                            if self._on_resumption_handle is not None:
                                await self._on_resumption_handle(
                                    resumption_update.new_handle, True
                                )
                        else:
                            if self._on_resumption_handle is not None:
                                await self._on_resumption_handle(
                                    resumption_update.new_handle, False
                                )

                    go_away = response.go_away
                    if go_away is not None:
                        time_left = go_away.time_left
                        seconds = self._parse_time_left(time_left)
                        await self._emit(SessionExpiring(seconds=seconds))
                        return DisconnectReason("go_away", time_left)

                    server_content = response.server_content
                    if server_content:
                        # generation_complete ends the model's generation; with
                        # realtime playback the API may delay turn_complete past
                        # it, so treat a standalone generation_complete as the
                        # same turn boundary (flush transcript, advance turn id).
                        if server_content.generation_complete:
                            logger.debug("generation_complete received")
                            if not server_content.turn_complete:
                                server_content = server_content.model_copy(
                                    update={"turn_complete": True}
                                )

                        # The user started talking: get one current frame in
                        # front of the model now rather than at the next
                        # heartbeat, so "what is this?" resolves against what
                        # is actually on screen. Unchanged screens still cost
                        # nothing (``_send_frame`` drops duplicates).
                        if (
                            self._settings.frame_mode == "on_demand"
                            and not self._frame_sent_this_utterance
                            and (
                                self._settings.share_screen
                                or self._settings.computer_use_enabled
                            )
                            and server_content.input_transcription
                            and server_content.input_transcription.text
                        ):
                            self._spawn_frame(session, reason="user_turn")

                        # Wake-word gate: a voice turn that never addressed us
                        # must produce no audio, no transcript, no tool calls.
                        if self._update_wake_gate(server_content):
                            if self._speaker is not None:
                                self._speaker.flush()
                            await self._emit(
                                SessionError(
                                    message=(
                                        "wake-word gate: ignored speech without "
                                        f"'{self._settings.wake_word}'"
                                    ),
                                    fatal=False,
                                )
                            )
                        gated = self._turn_gated
                        if (server_content.model_turn and server_content.model_turn.parts) or (
                            server_content.output_transcription
                            and server_content.output_transcription.text
                        ):
                            self._model_turn_active = True

                        # Barge-in: the server detected user speech and aborted
                        # the model turn. Kill queued playback first, before any
                        # other handling, so the model stops mid-sentence rather
                        # than finishing the response it already generated.
                        if server_content.interrupted:
                            # The interrupted response never delivers a first
                            # audio for the old speech end; start over.
                            self._speech_end_at = None
                            self._first_audio_seen = False
                            self._model_turn_active = False
                            self._user_interrupted = False
                        if server_content.interrupted and self._speaker is not None:
                            self._speaker.flush()

                        # Latest input transcription chunk approximates the end
                        # of the user's speech (see ``_speech_end_at``).
                        if (
                            server_content.input_transcription
                            and server_content.input_transcription.text
                            and not self._first_audio_seen
                        ):
                            self._speech_end_at = self._metrics.stamp()

                        # Extended Thinking can set turn_complete at the end of
                        # each spoken filler while background reasoning continues.
                        # Keep those chunks in TurnState until interaction_status
                        # returns to IDLE so they are persisted as one response.
                        turn_state_content = _turn_state_content(
                            self._settings.model, server_content
                        )
                        events = self._turn_state.consume_server_content(
                            turn_state_content
                        )
                        if (
                            _should_reset_turn_gate(self._settings.model, server_content)
                            and not self._pending_gate_calls
                        ):
                            # Held tool calls decide the turn themselves; do
                            # not force an empty-utterance verdict on them.
                            self._evaluate_wake_gate_if_needed()
                        self._queue_turn_events(events)

                        # Play audio contained in the model turn.
                        model_turn = server_content.model_turn
                        if (
                            not gated
                            and not self._user_interrupted
                            and model_turn
                            and model_turn.parts
                            and self._speaker is not None
                        ):
                            for part in model_turn.parts:
                                inline_data = getattr(part, "inline_data", None)
                                if inline_data and inline_data.data:
                                    with self._metrics.measure("audio_enqueue_ms"):
                                        self._speaker.write(inline_data.data)
                                    if not self._first_audio_seen:
                                        self._first_audio_seen = True
                                        if self._speech_end_at is not None:
                                            self._metrics.record_elapsed(
                                                "speech_end_to_first_audio_ms",
                                                self._speech_end_at,
                                            )
                                            self._speech_end_at = None
                                    self._metrics.record_elapsed(
                                        "receive_to_audio_queue_ms", received_at
                                    )

                    # Tool calls are handled off the receive path so approvals and
                    # blocking operations cannot stall message handling. A gated
                    # (unaddressed) turn must never dispatch tools.
                    #
                    # NON_BLOCKING declarations (Module 3) arrive through this
                    # same ``tool_call`` message — the SDK has no separate
                    # scheduled-call payload. Because every call is dispatched
                    # as an independent task, non-blocking tools already run
                    # without interrupting in-flight model audio; their
                    # responses carry ``scheduling`` (WHEN_IDLE/SILENT) so the
                    # server folds results in without hijacking the turn.
                    tool_call = response.tool_call
                    if tool_call and tool_call.function_calls:
                        await self._handle_tool_calls(
                            session, list(tool_call.function_calls)
                        )

                    cancellation = response.tool_call_cancellation
                    if cancellation and cancellation.ids:
                        cancelled = set(cancellation.ids)
                        # Held calls the server withdrew need no answer.
                        self._pending_gate_calls = [
                            fc for fc in self._pending_gate_calls
                            if fc.id not in cancelled
                        ]
                        self._addressed_call_ids -= cancelled
                        self._gate_input_changed.set()
                    if cancellation and self._dispatcher is not None:
                        self._dispatcher.cancel(cancellation.ids or [])

                    # Turn boundaries must only reset gating state after tool calls
                    # in the same response message have been evaluated and dispatched.
                    if server_content and _should_reset_turn_gate(
                        self._settings.model, server_content
                    ):
                        self._queue_turn_events(self._turn_state.flush())
                        self._on_turn_boundary_gate()
                        self._frame_utterance += 1
                        self._frame_sent_this_utterance = False
                        self._speech_end_at = None
                        self._first_audio_seen = False
                        self._model_turn_active = False
                        self._user_interrupted = False

                    usage = response.usage_metadata
                    if usage:
                        # genai >= 2.x: output tokens are ``response_token_count``
                        # and per-modality counts live in the *_tokens_details
                        # lists. Older field names (completion/audio/video)
                        # silently miss, so read the real ones.
                        def _modality_tokens(details: Any, name: str) -> int:
                            return sum(
                                getattr(d, "token_count", 0) or 0
                                for d in (details or [])
                                if _modality_name(getattr(d, "modality", "")) == name
                            )

                        prompt_details = getattr(usage, "prompt_tokens_details", None)
                        response_details = getattr(
                            usage, "response_tokens_details", None
                        )
                        audio_tokens = _modality_tokens(
                            prompt_details, "AUDIO"
                        ) + _modality_tokens(response_details, "AUDIO")
                        # Screen frames go out as ``image/jpeg`` blobs, and the
                        # API bills them back as IMAGE, not VIDEO — counting
                        # only VIDEO reports zero for a screen-sharing session.
                        video_tokens = sum(
                            _modality_tokens(details, modality)
                            for details in (prompt_details, response_details)
                            for modality in ("VIDEO", "IMAGE")
                        )
                        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
                        completion_tokens = (
                            getattr(usage, "response_token_count", 0) or 0
                        )
                        # Per-message usage_metadata is a per-turn increment
                        # (context is rebilled each turn), so counts are summed.
                        # Cumulative cost is at paid-tier rates; free-tier
                        # sessions (``free_tier``) report ``None`` so the GUI
                        # shows "Free".
                        self._prompt_tokens_used += prompt_tokens
                        self._completion_tokens_used += completion_tokens
                        input_rate, output_rate = _cost_rates_for_model(
                            self._settings.model
                        )
                        estimated_usd: float | None = (
                            self._prompt_tokens_used * input_rate
                            + self._completion_tokens_used * output_rate
                        ) / 1_000_000
                        if self._free_tier:
                            estimated_usd = None
                        usage_event = UsageUpdate(
                            prompt_tokens=prompt_tokens,
                            cached_tokens=getattr(
                                usage, "cached_content_token_count", 0
                            )
                            or 0,
                            completion_tokens=completion_tokens,
                            audio_tokens=audio_tokens,
                            video_tokens=video_tokens,
                            total_tokens=getattr(usage, "total_token_count", 0) or 0,
                            estimated_usd=estimated_usd,
                        )
                        await self._emit(usage_event)
                        self._record_usage(usage_event)
                        await self._emit_diagnostics()

        except asyncio.CancelledError:
            return None
        except Exception as exc:
            logger.exception("Receive loop failed")
            await self._emit(SessionError(message=str(exc), fatal=True))
            await self._maybe_emit_rate_limit(exc)
            return self._classify_disconnect(exc)

        return API_CLOSE

    async def _send_loop(self, session: Any) -> DisconnectReason | None:
        """Consume commands from the GUI/driver and forward them to the API."""
        try:
            while True:
                cmd = await self._command_queue.get()

                if isinstance(cmd, Disconnect):
                    return USER_REQUEST

                if isinstance(cmd, SendText):
                    if (
                        self._settings.frame_mode == "on_demand"
                        and (self._settings.share_screen or self._settings.computer_use_enabled)
                    ):
                        # Every typed turn can refer to the screen; inspect it
                        # before forwarding text, without guessing its meaning.
                        # Bounded: the shielded frame task keeps running (and
                        # is cancelled in ``_stop_media``) if we stop waiting.
                        try:
                            refreshed = await asyncio.wait_for(
                                asyncio.shield(
                                    self._spawn_frame(session, reason="user_turn")
                                ),
                                _FRAME_WAIT_SECONDS,
                            )
                        except TimeoutError:
                            logger.warning(
                                "[SCREEN] frame wait exceeded %.1fs; sending text without it",
                                _FRAME_WAIT_SECONDS,
                            )
                            self._metrics.increment("frame_wait_timeouts")
                            refreshed = True
                        if not refreshed:
                            await self._emit(
                                SessionError(
                                    message=("Could not refresh the screen; "
                                             "text was not sent. Try again."),
                                    fatal=False,
                                )
                            )
                            await self._emit(TextNotSent(
                                text=cmd.text, reason="Could not refresh the screen"
                            ))
                            continue
                    # TODO(Gemini 3.1): the docs suggest live text input may need
                    # to go through ``send_realtime_input`` rather than
                    # ``send_client_content``. Verify against a live socket.
                    await session.send_client_content(
                        turns=[
                            types.Content(
                                role="user",
                                parts=[types.Part(text=cmd.text)],
                            )
                        ],
                        turn_complete=True,
                    )
                    # Typed input is always addressed to the agent, so the next
                    # model turn is exempt from wake-word gating.
                    self._text_turn_pending = True

                elif isinstance(cmd, Interrupt):
                    if self._speaker is not None:
                        self._speaker.flush()
                    # Only a reply that is streaming, or owed for speech that
                    # just ended, needs its remaining audio dropped. After the
                    # turn boundary the flush alone stops the buffered tail,
                    # and the next turn must still play.
                    self._user_interrupted = (
                        self._model_turn_active or self._speech_end_at is not None
                    )
                    logger.info(
                        "[INTERRUPT] user stop (mid-turn=%s)", self._user_interrupted
                    )

                elif isinstance(cmd, SetMicGate):
                    was_open = self._mic_gate_open
                    self._mic_gate_open = cmd.open
                    if self._mic is not None:
                        self._mic.set_gate(cmd.open)
                    if was_open and not cmd.open:
                        # Muting ends the user's audio stream; tell the API
                        # inline (not queued behind audio chunks) so VAD
                        # finalizes the utterance within ~50ms of the mute.
                        self._audio_streamed = False
                        if not self._first_audio_seen:
                            self._speech_end_at = self._metrics.stamp()
                        try:
                            await session.send_realtime_input(audio_stream_end=True)
                        except Exception as exc:
                            logger.warning("audio_stream_end send failed: %s", exc)

                elif isinstance(cmd, SetShareScreen):
                    if (
                        cmd.monitor is not None
                        and cmd.monitor != self._screen_monitor
                    ):
                        try:
                            if self._screen is not None:
                                await self._screen.set_monitor(cmd.monitor)
                            else:
                                await asyncio.to_thread(validate_monitor, cmd.monitor)
                        except Exception as exc:
                            await self._emit(
                                SessionError(
                                    message=f"Could not select screen monitor {cmd.monitor}: {exc}",
                                    fatal=False,
                                )
                            )
                        else:
                            self._screen_monitor = cmd.monitor
                            computer_tools.set_monitor(cmd.monitor)
                            self._last_frame_sent = 0.0
                            self._last_frame_digest = None

                    if not cmd.enabled and self._settings.computer_use_enabled:
                        logger.warning(
                            "[SCREEN] Ignoring SetShareScreen(False) because "
                            "computer_use is enabled; the model must see the screen."
                        )
                        continue
                    if cmd.enabled:
                        if (
                            self._settings.frame_mode == "on_demand"
                            and self._screen is None
                        ):
                            # No capture thread in on-demand mode; the frame
                            # task grabs its own frames.
                            if (
                                self._video_send_task is None
                                or self._video_send_task.done()
                            ):
                                self._video_send_task = asyncio.create_task(
                                    self._video_send_loop(session, None),
                                    name="video_send_dynamic",
                                )
                        elif self._screen is None:
                            self._screen = ScreenCapture(
                                fps=self._settings.screen_fps,
                                monitor=self._screen_monitor,
                                computer_use=self._settings.computer_use_enabled,
                                max_bytes=DEFAULT_MAX_BYTES,
                            )
                            self._owns_screen = True
                            await self._screen.start()
                            self._video_send_task = asyncio.create_task(
                                self._video_send_loop(session, self._screen),
                                name="video_send_dynamic",
                            )
                        elif self._video_send_task is None or self._video_send_task.done():
                            self._video_send_task = asyncio.create_task(
                                self._video_send_loop(session, self._screen),
                                name="video_send_dynamic",
                            )
                    else:
                        if self._video_send_task is not None and not self._video_send_task.done():
                            self._video_send_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await self._video_send_task
                            self._video_send_task = None
                        if self._screen is not None and self._owns_screen:
                            await self._screen.stop()
                            self._screen = None

                elif isinstance(cmd, ApproveTool):
                    if self._dispatcher is not None:
                        self._dispatcher.approve(cmd.call_id)

                elif isinstance(cmd, DenyTool):
                    if self._dispatcher is not None:
                        self._dispatcher.deny(cmd.call_id, reason=cmd.reason)

                elif isinstance(cmd, CancelTool):
                    if self._dispatcher is not None:
                        self._dispatcher.cancel(cmd.call_ids)

                elif isinstance(cmd, SetGatingMode):
                    if self._dispatcher is not None:
                        self._dispatcher.set_gating_mode(cmd.mode)
                    self._journal.gating_mode_change(
                        cmd.mode.value,
                        duration_minutes=cmd.duration_minutes,
                    )

                else:
                    logger.warning("Ignored unknown command type: %s", type(cmd))

        except asyncio.CancelledError:
            return None
        except Exception as exc:
            logger.exception("Send loop failed")
            await self._emit(SessionError(message=str(exc), fatal=True))
            await self._maybe_emit_rate_limit(exc)
            return self._classify_disconnect(exc)

    async def _audio_send_loop(self, session: Any, mic: Microphone) -> None:
        """Stream microphone PCM to the model."""
        try:
            async for chunk in mic.chunks():
                if self._shutdown_event.is_set():
                    break
                if not self._mic_gate_open:
                    continue
                try:
                    with self._metrics.measure("audio_send_ms"):
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=chunk,
                                mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                            )
                        )
                    self._audio_streamed = True
                    self._metrics.increment("audio_chunks_sent")
                    self._metrics.increment("audio_bytes_sent", len(chunk))
                except Exception as exc:
                    logger.warning("Audio send failed: %s", exc)
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Audio send loop failed")
            await self._emit(SessionError(message=str(exc), fatal=False))

    async def _heartbeat_frame_loop(self, session: Any) -> None:
        """Send an ambient frame every ``frame_heartbeat_seconds``.

        On-demand counterpart of ``_video_send_loop``: the model still sees the
        screen change without a frame going out every second. Unchanged screens
        are dropped by ``_send_frame``, so an idle desktop sends nothing.
        """
        try:
            while not self._shutdown_event.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self._settings.frame_heartbeat_seconds,
                    )
                if self._shutdown_event.is_set():
                    return
                try:
                    await self._send_frame(session, reason="heartbeat")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Heartbeat frame failed: %s", exc)
        except asyncio.CancelledError:
            pass

    async def _video_send_loop(
        self, session: Any, screen: ScreenCapture | None
    ) -> None:
        """Stream screen JPEG frames to the model.

        In ``on_demand`` frame mode there is no capture thread and ``screen``
        is ``None``: frames come from the heartbeat and from computer-use
        actions instead.
        """
        if self._settings.frame_mode == "on_demand" or screen is None:
            await self._heartbeat_frame_loop(session)
            return

        consecutive_errors = 0
        try:
            while not self._shutdown_event.is_set():
                try:
                    frame = await screen.read_frame()
                    previous_send = self._last_frame_sent
                    async with self._frame_lock:
                        # A requested observation that won the lock has already
                        # replaced this queued stream frame with newer context.
                        if previous_send != self._last_frame_sent:
                            continue
                        await self._send_frame_bytes(
                            session, frame,
                            force=not self._settings.screen_deduplicate,
                            refresh=self._settings.screen_deduplicate,
                        )
                    consecutive_errors = 0
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    consecutive_errors += 1
                    logger.warning(
                        "Video send failed (%d/%d): %s",
                        consecutive_errors,
                        _MAX_VIDEO_SEND_ERRORS,
                        exc,
                    )
                    if consecutive_errors >= _MAX_VIDEO_SEND_ERRORS:
                        logger.error(
                            "Too many video send errors; stopping screen share."
                        )
                        break
                    await asyncio.sleep(1.0)
        except Exception as exc:
            logger.exception("Video send loop failed")
            await self._emit(SessionError(message=str(exc), fatal=False))

    async def _meter_loop(self) -> None:
        """Poll media levels and emit ``AudioLevel`` events."""
        try:
            while True:
                if self._shutdown_event.is_set():
                    return

                if self._mic is not None:
                    await self._emit_level("mic", self._mic.level)
                if self._speaker is not None:
                    await self._emit_level("speaker", self._speaker.level)

                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=_METER_INTERVAL_SECONDS,
                    )
        except asyncio.CancelledError:
            pass

    async def _emit_level(self, source: str, level: float) -> None:
        """Emit ``AudioLevel`` only when changed, at most 15 Hz per source."""
        now = self._metrics.stamp()
        last = self._last_meter.get(source)
        if last is not None and (
            level == last[0] or now - last[1] < _METER_EMIT_MIN_INTERVAL_SECONDS
        ):
            return
        self._last_meter[source] = (level, now)
        await self._emit(AudioLevel(source=source, level=level))

    def _make_respond_callback(
        self, session: Any
    ) -> Callable[[str, str, ToolResult], Awaitable[bool]]:
        """Return the callback used by ``ToolDispatcher`` to reply to tool calls."""

        async def respond(call_id: str, name: str, result: ToolResult) -> bool:
            await session.send_tool_response(
                function_responses=[
                    types.FunctionResponse(
                        id=call_id,
                        name=name,
                        response={"result": result.message},
                        scheduling=_response_scheduling_for(
                            name, self._settings.model
                        ),
                    )
                ]
            )
            self._note_tool_response_sent(call_id)
            if name in COMPUTER_TOOL_NAMES:
                await self._push_fresh_frame(session)
            return True

        return respond

    async def _send_frame(self, session: Any, *, reason: str, force: bool = False) -> bool:
        """Send at most one screen frame, shared by every frame trigger.

        Ambient captures have a minimum interval. User turns bypass that floor
        to check the current screen even just after a heartbeat, but retain the
        digest check. This can send changed frames closer together in exchange
        for prompt user context. ``force`` bypasses both gates, and
        is used for the post-action frame: that frame is the model's only way
        to see what its action did, including an action that changed nothing,
        and it must not be lost because a heartbeat happened to fire just
        before it.
        """
        async with self._frame_lock:
            now = time.monotonic()
            if (
                not force
                and reason != "user_turn"
                and now - self._last_frame_sent < self._settings.frame_min_interval_seconds
            ):
                return False

            with self._metrics.measure("frame_capture_ms"):
                # Pixel digest compare happens inside the capture so an unchanged
                # screen skips the JPEG encode as well as the send.
                frame = await asyncio.to_thread(
                    capture_jpeg,
                    self._screen_monitor,
                    prev_digest=None if force else self._last_frame_digest,
                    computer_use=self._settings.computer_use_enabled,
                    max_bytes=DEFAULT_MAX_BYTES,
                )
            if getattr(frame, "unchanged", False):
                self._metrics.increment("frame_duplicates_dropped")
                logger.debug("[SCREEN] frame checked (%s, sent=False)", reason)
                return False
            sent = await self._send_frame_bytes(session, frame, force=force)
            logger.debug("[SCREEN] frame checked (%s, sent=%s)", reason, sent)
            return sent

    async def _send_frame_bytes(
        self, session: Any, frame: bytes, *, force: bool = False, refresh: bool = False
    ) -> bool:
        """Send under ``_frame_lock``; commit digest/metrics only after success."""
        digest = getattr(frame, "pixel_digest", None) or hashlib.blake2b(
            frame, digest_size=16
        ).digest()
        refresh_due = (
            refresh
            and time.monotonic() - self._last_frame_sent >= self._settings.frame_heartbeat_seconds
        )
        if not force and not refresh_due and digest == self._last_frame_digest:
            self._metrics.increment("frame_duplicates_dropped")
            return False

        with self._metrics.measure("frame_send_ms"):
            await session.send_realtime_input(
                video=types.Blob(data=frame, mime_type="image/jpeg")
            )
        self._last_video_sent_at = self._metrics.stamp()
        self._metrics.increment("frames_sent")
        self._metrics.increment("frame_bytes_sent", len(frame))
        self._last_frame_sent = time.monotonic()
        self._last_frame_digest = digest
        return True

    def _spawn_frame(self, session: Any, *, reason: str) -> asyncio.Task[bool]:
        """Coalesce pending user captures and leave failed utterances retryable."""
        if (
            reason == "user_turn"
            and self._user_frame_task is not None
            and not self._user_frame_task.done()
        ):
            return self._user_frame_task
        utterance = self._frame_utterance

        async def send() -> bool:
            try:
                await self._send_frame(session, reason=reason)
                if reason == "user_turn" and utterance == self._frame_utterance:
                    # An unchanged frame is a successful freshness check too.
                    self._frame_sent_this_utterance = True
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Frame send failed (%s): %s", reason, exc)
                return False

        task = asyncio.create_task(send(), name=f"frame_{reason}")
        if reason == "user_turn":
            self._user_frame_task = task
        self._frame_tasks.add(task)
        task.add_done_callback(self._frame_tasks.discard)
        return task

    async def _push_fresh_frame(self, session: Any) -> None:
        """Send a one-shot screenshot after a computer-use action.

        Live tool responses cannot carry images, so the updated screen state
        rides the realtime video channel instead. The short delay lets the
        desktop react to the action before the frame is grabbed. Failures
        here must not break the tool response that was already delivered.
        """
        try:
            await asyncio.sleep(_POST_ACTION_SETTLE_SECONDS)
            await self._send_frame(session, reason="post_action", force=True)
        except Exception as exc:
            logger.warning("Failed to push post-action screenshot: %s", exc)

    def _classify_disconnect(self, exc: BaseException) -> DisconnectReason:
        """Map an exception to a structured disconnect reason."""
        detail = str(exc)
        if isinstance(exc, ValueError):
            if detail == "No API key configured":
                return DisconnectReason("auth_error", detail)
            return DisconnectReason("invalid_request", detail)
        if isinstance(exc, google.genai.errors.APIError):
            status = getattr(exc, "status", None)
            code = getattr(exc, "code", None)
            if status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
                return DisconnectReason("auth_error", detail)
            if status == "NOT_FOUND":
                return DisconnectReason("not_found", detail)
            if status == "RESOURCE_EXHAUSTED" or code == 429:
                return DisconnectReason("quota", detail)
            if (
                status in ("INVALID_ARGUMENT", "FAILED_PRECONDITION", "UNIMPLEMENTED")
                or code == 400
            ):
                return DisconnectReason("invalid_request", detail)
            if status in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED", "UNKNOWN"):
                return DisconnectReason("transient", detail)

        lowered = detail.lower()
        if "resumption handle" in lowered and "rejected" in lowered:
            return DisconnectReason("rejected_handle", detail)

        if isinstance(exc, (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError)):
            return DisconnectReason("transient", detail)

        return DisconnectReason("transient", detail)

    @staticmethod
    def _is_rate_limit(exc: BaseException) -> bool:
        """Return True if ``exc`` looks like a rate-limit (429) termination.

        The Live API signals quota exhaustion two ways (see ``genai/live.py``):
        an in-band error message, which the SDK raises as ``ClientError`` with
        ``code == 429`` and ``status == "RESOURCE_EXHAUSTED"``, or a WebSocket
        close whose reason string the SDK wraps in a plain ``APIError`` (close
        codes are 1xxx, so no subclass), leaving only the message text to
        match on.
        """
        if isinstance(exc, google.genai.errors.APIError):
            if getattr(exc, "status", None) == "RESOURCE_EXHAUSTED":
                return True
            if getattr(exc, "code", None) == 429:
                return True
        lowered = str(exc).lower()
        return (
            "resource_exhausted" in lowered
            or "429" in lowered
            or "rate limit" in lowered
            or "quota" in lowered
        )

    async def _maybe_emit_rate_limit(self, exc: BaseException) -> None:
        """Emit ``RateLimitHit`` exactly once per session on a 429 failure.

        The session does not know which tier it is running on or the tier's
        TPM counters; the supervisor fills those in from its own tracking.
        """
        if self._rate_limit_emitted or not self._is_rate_limit(exc):
            return
        self._rate_limit_emitted = True
        await self._emit(
            RateLimitHit(
                model=self._settings.model,
                tier="",
                tpm_used=0,
                tpm_limit=0,
                cooldown_seconds=0.0,
            )
        )

    def _parse_time_left(self, time_left: str | None) -> int | None:
        """Parse a duration string such as ``'60s'`` into seconds."""
        if time_left is None:
            return None
        match = re.search(r"\d+", time_left)
        if match is None:
            return None
        return int(match.group())

    async def send_client_content(
        self, turns: list[types.Content], turn_complete: bool = True
    ) -> None:
        """Forward client content to the active API session."""
        if self._session is None:
            raise RuntimeError("not connected")
        await self._session.send_client_content(
            turns=turns, turn_complete=turn_complete
        )

    async def send_tool_response(self, call_id: str, name: str, result: ToolResult) -> None:
        """Send a tool response to the active API session.

        Computer-use calls also push a fresh screenshot so the model sees the
        result of its action; this is the path the supervisor's respond
        callback funnels through, so the push must live here rather than only
        in ``_make_respond_callback``.
        """
        if self._session is None:
            raise RuntimeError("not connected")
        await self._session.send_tool_response(
            function_responses=[
                types.FunctionResponse(
                    id=call_id,
                    name=name,
                    response={"result": result.message},
                    scheduling=_response_scheduling_for(name, self._settings.model),
                )
            ]
        )
        self._note_tool_response_sent(call_id)
        if name in COMPUTER_TOOL_NAMES:
            await self._push_fresh_frame(self._session)

    async def _emit(self, event: Any) -> None:
        await self._event_queue.put(event)

    def runtime_diagnostics(self) -> DiagnosticsUpdate:
        """Snapshot local spans and media queues without retaining user content."""
        audio: dict[str, float] = {}
        for name, media in (("mic", self._mic), ("speaker", self._speaker)):
            for key, value in getattr(media, "diagnostics", {}).items():
                audio[f"{name}.{key}"] = float(value)
        if self._last_video_sent_at is not None:
            audio["last_frame_send_age_ms"] = max(
                0.0, self._metrics.stamp() - self._last_video_sent_at
            ) * 1000
        return DiagnosticsUpdate(
            timings=self._metrics.snapshot(), audio=audio, counters=self._metrics.counters()
        )

    async def _emit_diagnostics(self, *, force: bool = False) -> None:
        """Publish ``runtime_diagnostics`` at most once per second unless forced."""
        now = self._metrics.stamp()
        if (
            not force
            and self._last_diagnostics_at is not None
            and now - self._last_diagnostics_at < _DIAGNOSTICS_MIN_INTERVAL_SECONDS
        ):
            return
        self._last_diagnostics_at = now
        await self._emit(self.runtime_diagnostics())

    async def _diagnostics_loop(self) -> None:
        """Sample local event-loop lag once per second; publish with usage only."""
        while not self._shutdown_event.is_set():
            due = self._metrics.stamp() + 1.0
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=1.0)
            except TimeoutError:
                self._metrics.record_elapsed("loop_lag_ms", due)

    def _record_usage(self, event: UsageUpdate) -> None:
        """Persist non-zero usage modalities as incremental history rows."""
        recorded_at = datetime.now(UTC).isoformat()
        input_rate, output_rate = _cost_rates_for_model(self._settings.model)
        if self._free_tier or event.estimated_usd is None or event.estimated_usd == 0.0:
            prompt_usd = 0.0
            completion_usd = 0.0
        else:
            prompt_usd = (event.prompt_tokens * input_rate) / 1_000_000
            completion_usd = (event.completion_tokens * output_rate) / 1_000_000

        modalities = {
            "prompt": (event.prompt_tokens, prompt_usd),
            "cached": (event.cached_tokens, 0.0),
            "completion": (event.completion_tokens, completion_usd),
            "audio": (event.audio_tokens, 0.0),
            "video": (event.video_tokens, 0.0),
        }
        rows = [
            (modality, tokens, modality_usd)
            for modality, (tokens, modality_usd) in modalities.items()
            if tokens
        ]
        with self._metrics.measure("usage_persist_ms"):
            self._store.record_usage_batch(self._run_id, rows, recorded_at)

    async def _send_audio_stream_end(self, session: Any) -> None:
        """Best-effort ``audio_stream_end`` marker so VAD finalizes promptly.

        Only sent when audio has streamed since the last end marker; failures
        are swallowed because the socket may already be broken.
        """
        if not self._audio_streamed:
            return
        self._audio_streamed = False
        try:
            await session.send_realtime_input(audio_stream_end=True)
        except Exception as exc:
            logger.warning("audio_stream_end send failed: %s", exc)

    async def _stop_media(self) -> None:
        """Stop microphone, speaker, and screen capture if the session owns them."""
        if self._frame_tasks:
            tasks = list(self._frame_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._frame_tasks.clear()
        self._user_frame_task = None

        if self._screen is not None and self._owns_screen:
            try:
                await self._screen.stop()
            except Exception as exc:
                logger.warning("Screen stop failed: %s", exc)
            self._screen = None

        if self._mic is not None and self._owns_mic:
            try:
                await self._mic.stop()
            except Exception as exc:
                logger.warning("Mic stop failed: %s", exc)
            self._mic = None

        if self._speaker is not None and self._owns_speaker:
            try:
                await self._speaker.stop()
            except Exception as exc:
                logger.warning("Speaker stop failed: %s", exc)
            self._speaker = None

    def _audio_output_available(self) -> bool:
        try:
            return len(devices.list_output_devices()) > 0
        except Exception:
            return False

    def _audio_input_available(self) -> bool:
        try:
            return len(devices.list_input_devices()) > 0
        except Exception:
            return False


# Re-export so callers can import the reason type from this module as well.
__all__ = ["LiveSession"]
