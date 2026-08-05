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
    SendText,
    SetGatingMode,
    SetMicGate,
    SetShareScreen,
)
from core.dispatcher import ToolDispatcher
from core.events import (
    API_CLOSE,
    USER_REQUEST,
    AudioLevel,
    ConnectionState,
    ConnectionStateChanged,
    DisconnectReason,
    PartialTranscript,
    RateLimitHit,
    SessionError,
    SessionExpiring,
    TranscriptSource,
    TurnComplete,
    UsageUpdate,
)
from core.turn_state import TurnState
from core.wake_word import contains_wake_word
from media import devices
from media.microphone import INPUT_SAMPLE_RATE, Microphone
from media.screen import ScreenCapture, capture_jpeg
from media.speaker import Speaker
from obs.logging_setup import get_logger
from persist.store import Store
from policy import PolicyEngine
from settings.secrets import get_api_key
from settings.settings import AppSettings
from tools import build_function_declarations
from tools import computer as computer_tools
from tools import registry as tool_registry
from tools._state import set_policy_engine, set_workspace_root
from tools.registry import COMPUTER_TOOL_NAMES, ToolResult

logger = get_logger(__name__)

_MAX_VIDEO_SEND_ERRORS = 5
_METER_INTERVAL_SECONDS = 0.033
# Time given to the desktop to repaint after a computer-use action before the
# post-action frame is grabbed.
_POST_ACTION_SETTLE_SECONDS = 0.15

# Per-1M-token pricing (input USD, output USD) for the Live models, keyed by
# model-name prefix. The session does not know which pricing tier it is
# running on (the supervisor does), so it always computes the paid-tier
# estimate; free-tier sessions should surface ``estimated_usd=None`` on the
# ``UsageUpdate`` event so the GUI can show "Free" instead of a dollar figure.
_COST_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-live-preview": (0.15, 0.60),
    "gemini-2.5-flash-native-audio": (0.15, 0.60),
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

_MEDIA_RESOLUTION_BY_SETTING: dict[str, types.MediaResolution] = {
    "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
    "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
}

_THINKING_LEVEL_BY_SETTING: dict[str, types.ThinkingLevel] = {
    "minimal": types.ThinkingLevel.MINIMAL,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}


def _supports_search_and_affective(model: str) -> bool:
    """Whether the model accepts google_search and affective dialog.

    Verified live against the API: ``gemini-3.1-flash-live-preview`` rejects
    both at connect time (1011 quota/internal errors); the 2.5 native-audio
    family accepts them.
    """
    return model.startswith("gemini-2.5-flash-native-audio")


def _build_tools(settings: AppSettings) -> list[types.Tool]:
    """Build the tool list, preferring the registry's consolidated builder.

    ``build_tools`` (single consolidated ``Tool`` plus conditional
    google_search / code_execution tools) supersedes the per-declaration
    ``build_function_declarations`` entry point; fall back to the legacy path
    when the registry does not expose it yet. google_search is dropped on
    models that reject it (see ``_supports_search_and_affective``).
    """
    build_tools = getattr(tool_registry, "build_tools", None)
    if build_tools is not None:
        tools = list(build_tools(settings))
    else:
        tools = list(
            build_function_declarations(computer_use=settings.computer_use_enabled)
        )
    if not _supports_search_and_affective(settings.model):
        tools = [tool for tool in tools if tool.google_search is None]
    return tools


def _tool_schedules() -> dict[str, str]:
    """Return the registry's ``FUNCTION_SCHEDULES`` (empty if unavailable)."""
    schedules = getattr(tool_registry, "FUNCTION_SCHEDULES", None)
    return dict(schedules) if schedules else {}


def _apply_function_behavior(tools: list[types.Tool]) -> list[types.Tool]:
    """Stamp each declaration with the ``Behavior`` its schedule implies."""
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


def _response_scheduling_for(name: str) -> types.FunctionResponseScheduling | None:
    """Scheduling to attach to a tool response, or ``None`` for blocking tools."""
    schedule = _tool_schedules().get(name)
    if schedule is None:
        return None
    return _RESPONSE_SCHEDULING_BY_SCHEDULE.get(schedule)


def _session_config_fields(settings: AppSettings) -> dict[str, Any]:
    """Module-3 ``LiveConnectConfig`` fields layered over ``as_connect_config``.

    Note: ``safety_settings`` exists on the SDK's ``LiveConnectConfig`` but the
    Live API backend rejects it (``Unknown name "safetySettings" at 'setup'``),
    so it is deliberately not sent.
    """
    return {
        "media_resolution": _MEDIA_RESOLUTION_BY_SETTING[settings.media_resolution],
        "enable_affective_dialog": settings.enable_affective_dialog
        and _supports_search_and_affective(settings.model),
        "thinking_config": types.ThinkingConfig(
            thinking_level=_THINKING_LEVEL_BY_SETTING[settings.thinking_level]
        ),
    }


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
    ) -> None:
        self._settings = settings
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

    async def run(self) -> DisconnectReason:
        """Connect to the API, spawn loops, and return when the session ends."""
        policy = PolicyEngine(
            allow_roots=[self._settings.workspace_root],
            deny_globs=[r"*\.git\*", "__pycache__"],
            never_allow_regex=[
                r"AIza[0-9A-Za-z\-_]{35}",
                r"sk-[A-Za-z0-9]{20,}",
                r"ghp_[A-Za-z0-9]{36}",
            ],
        )
        set_policy_engine(policy)
        set_workspace_root(self._settings.workspace_root)
        computer_tools.set_monitor(self._settings.screen_monitor)

        api_key = get_api_key()
        if api_key is None:
            raise ValueError("No API key configured")

        client = genai.Client(api_key=api_key)
        connect_config = self._settings.as_connect_config(
            tools=_apply_function_behavior(_build_tools(self._settings)),
            resumption_handle=self._resumption_handle,
        )
        connect_config = connect_config.model_copy(
            update=_session_config_fields(self._settings)
        )

        await self._emit(ConnectionStateChanged(ConnectionState.CONNECTING))

        try:
            async with client.aio.live.connect(
                model=self._settings.model,
                config=connect_config,
            ) as session:
                self._session = session
                await self._emit(ConnectionStateChanged(ConnectionState.LIVE))
                self._journal.connection_event(
                    "connected", self._settings.model, epoch=self._epoch
                )
                if self._on_connected is not None:
                    await self._on_connected(self)

                reason = await self._run_session(session, policy)
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
            return reason
        finally:
            await self._emit(ConnectionStateChanged(ConnectionState.DISCONNECTED))
            self._journal.connection_event("disconnected", "", epoch=self._epoch)
            self._session = None
            self._closed = True

    async def _run_session(
        self, session: Any, policy: PolicyEngine
    ) -> DisconnectReason:
        """Start media and the concurrent send/receive loops."""
        if self._dispatcher is None:
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

        try:
            if self._audio_output_available() and self._speaker is None:
                self._speaker = Speaker(device=self._settings.output_device)
                await self._speaker.start()

            if (
                self._settings.mode != "text"
                and self._audio_input_available()
                and self._mic is None
            ):
                self._mic = Microphone(device=self._settings.input_device)
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
                )
                await self._screen.start()

            tasks: set[asyncio.Task[Any]] = set()
            tasks.add(
                asyncio.create_task(
                    self._receive_loop(session),
                    name="receive",
                )
            )
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

            # Signal the remaining loops to exit, then cancel them.
            self._shutdown_event.set()
            await self._stop_media()

            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            # Tell the API the audio stream ended before the WebSocket closes
            # so VAD can finalize any in-flight utterance. Best-effort: the
            # session may already be broken.
            await self._send_audio_stream_end(session)

            # Flush any partial transcript that arrived just before close.
            for event in self._turn_state.flush():
                await self._emit(event)

            return reason

        finally:
            await self._stop_media()

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

        has_model_output = bool(
            (server_content.model_turn and server_content.model_turn.parts)
            or (
                server_content.output_transcription
                and server_content.output_transcription.text
            )
        )
        if has_model_output and not self._turn_evaluated:
            self._turn_evaluated = True
            addressed = self._text_turn_pending or contains_wake_word(
                self._utterance_text,
                self._settings.wake_word,
                self._settings.wake_word_aliases,
            )
            # Log the transcript either way. A gated turn produces no audio and
            # no transcript bubble, so without this line "the agent ignored me"
            # is indistinguishable from "the agent never heard me", and the one
            # piece of evidence that separates them — what ASR actually
            # returned — is discarded.
            logger.info(
                "[WAKE] %s wake=%r aliases=%r heard=%r",
                "ADDRESSED" if addressed else "GATED (no response sent)",
                self._settings.wake_word,
                self._settings.wake_word_aliases,
                self._utterance_text.strip()[:200],
            )
            if not addressed:
                self._turn_gated = True
                return True
        return False

    def _reset_wake_gate(self) -> None:
        """Clear per-turn gating state at a turn boundary."""
        self._utterance_text = ""
        self._text_turn_pending = False
        self._turn_evaluated = False
        self._turn_gated = False

    async def _receive_loop(self, session: Any) -> DisconnectReason | None:
        """Consume server responses and dispatch side effects.

        ``session.receive()`` returns at the end of each model turn, so the
        outer ``while`` re-enters it for the next turn. This matches the
        Gemini Live SDK behavior and the original ``agent.py`` loop.
        """
        try:
            while not self._shutdown_event.is_set():
                async for response in session.receive():
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
                            self._frame_sent_this_utterance = True
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

                        # Barge-in: the server detected user speech and aborted
                        # the model turn. Kill queued playback first, before any
                        # other handling, so the model stops mid-sentence rather
                        # than finishing the response it already generated.
                        if server_content.interrupted and self._speaker is not None:
                            self._speaker.flush()

                        events = self._turn_state.consume_server_content(server_content)
                        for event in events:
                            if (
                                gated
                                and isinstance(event, (PartialTranscript, TurnComplete))
                                and event.source == TranscriptSource.MODEL
                            ):
                                continue  # gated turn: no agent transcript bubble
                            await self._emit(event)
                            if isinstance(event, TurnComplete):
                                completed_at = datetime.now(UTC).isoformat()
                                unique_id = (
                                    f"{self._run_id}-{self._epoch}-{event.turn_id}"
                                )
                                self._store.append_turn(
                                    unique_id,
                                    self._run_id,
                                    event.source.value,
                                    event.text,
                                    completed_at,
                                    self._epoch,
                                )

                        # Play audio contained in the model turn.
                        model_turn = server_content.model_turn
                        if (
                            not gated
                            and model_turn
                            and model_turn.parts
                            and self._speaker is not None
                        ):
                            for part in model_turn.parts:
                                inline_data = getattr(part, "inline_data", None)
                                if inline_data and inline_data.data:
                                    self._speaker.write(inline_data.data)

                        if server_content.turn_complete or server_content.interrupted:
                            self._reset_wake_gate()
                            self._frame_sent_this_utterance = False

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
                    if tool_call and tool_call.function_calls and not self._turn_gated:
                        for fc in tool_call.function_calls:
                            if self._dispatcher is not None:
                                # Tracked by the dispatcher, not by this
                                # session: the call must outlive a dropped
                                # socket so its result reaches whichever
                                # session is connected when it finishes.
                                self._dispatcher.spawn(
                                    self._dispatcher.submit(
                                        fc.id,
                                        fc.name,
                                        fc.args or {},
                                    )
                                )

                    cancellation = response.tool_call_cancellation
                    if cancellation and self._dispatcher is not None:
                        self._dispatcher.cancel(cancellation.ids or [])

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
                        # Cumulative session cost at paid-tier rates. The
                        # session cannot tell free from paid tier (the
                        # supervisor knows); the supervisor/GUI should null
                        # ``estimated_usd`` for free-tier sessions so the GUI
                        # shows "Free".
                        self._prompt_tokens_used += prompt_tokens
                        self._completion_tokens_used += completion_tokens
                        input_rate, output_rate = _cost_rates_for_model(
                            self._settings.model
                        )
                        estimated_usd = (
                            self._prompt_tokens_used * input_rate
                            + self._completion_tokens_used * output_rate
                        ) / 1_000_000
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
                        try:
                            await session.send_realtime_input(audio_stream_end=True)
                        except Exception as exc:
                            logger.warning("audio_stream_end send failed: %s", exc)

                elif isinstance(cmd, SetShareScreen):
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
                                monitor=cmd.monitor
                                if cmd.monitor is not None
                                else self._settings.screen_monitor,
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
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=chunk,
                            mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                        )
                    )
                    self._audio_streamed = True
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
                    await session.send_realtime_input(
                        video=types.Blob(data=frame, mime_type="image/jpeg")
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
                    await self._emit(
                        AudioLevel(source="mic", level=self._mic.level)
                    )
                if self._speaker is not None:
                    await self._emit(
                        AudioLevel(source="speaker", level=self._speaker.level)
                    )

                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=_METER_INTERVAL_SECONDS,
                    )
        except asyncio.CancelledError:
            pass

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
                        scheduling=_response_scheduling_for(name),
                    )
                ]
            )
            if name in COMPUTER_TOOL_NAMES:
                await self._push_fresh_frame(session)
            return True

        return respond

    async def _send_frame(self, session: Any, *, reason: str, force: bool = False) -> bool:
        """Send at most one screen frame, shared by every frame trigger.

        Two gates keep the context small: a floor on the interval between any
        two frames, and a digest check that drops a frame identical to the last
        one sent — an idle screen costs nothing. ``force`` bypasses both, and
        is used for the post-action frame: that frame is the model's only way
        to see what its action did, including an action that changed nothing,
        and it must not be lost because a heartbeat happened to fire just
        before it.
        """
        now = time.monotonic()
        if (
            not force
            and now - self._last_frame_sent < self._settings.frame_min_interval_seconds
        ):
            return False

        frame = await asyncio.to_thread(capture_jpeg, self._settings.screen_monitor)
        digest = hashlib.blake2b(frame, digest_size=16).digest()
        if not force and digest == self._last_frame_digest:
            logger.debug("[SCREEN] frame unchanged, not sent (%s)", reason)
            return False

        await session.send_realtime_input(
            video=types.Blob(data=frame, mime_type="image/jpeg")
        )
        self._last_frame_sent = now
        self._last_frame_digest = digest
        logger.debug("[SCREEN] frame sent (%s, %d bytes)", reason, len(frame))
        return True

    def _spawn_frame(self, session: Any, *, reason: str) -> None:
        """Fire a frame send off the receive loop, ignoring failures."""

        async def send() -> None:
            try:
                await self._send_frame(session, reason=reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Frame send failed (%s): %s", reason, exc)

        task = asyncio.ensure_future(send())
        self._frame_tasks.add(task)
        task.add_done_callback(self._frame_tasks.discard)

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
        if isinstance(exc, google.genai.errors.APIError):
            status = getattr(exc, "status", None)
            code = getattr(exc, "code", None)
            if status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
                return DisconnectReason("auth_error", detail)
            if status == "NOT_FOUND":
                return DisconnectReason("not_found", detail)
            if status == "RESOURCE_EXHAUSTED" or code == 429:
                return DisconnectReason("quota", detail)
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
                    scheduling=_response_scheduling_for(name),
                )
            ]
        )
        if name in COMPUTER_TOOL_NAMES:
            await self._push_fresh_frame(self._session)

    async def _emit(self, event: Any) -> None:
        await self._event_queue.put(event)

    def _record_usage(self, event: UsageUpdate) -> None:
        """Persist non-zero usage modalities as incremental history rows."""
        recorded_at = datetime.now(UTC).isoformat()
        # The usage table is NOT NULL on usd; unknown (free tier) is 0.0.
        usd = event.estimated_usd if event.estimated_usd is not None else 0.0
        modalities = {
            "prompt": event.prompt_tokens,
            "cached": event.cached_tokens,
            "completion": event.completion_tokens,
            "audio": event.audio_tokens,
            "video": event.video_tokens,
        }
        for modality, tokens in modalities.items():
            if tokens:
                self._store.record_usage(
                    self._run_id, modality, tokens, usd, recorded_at
                )

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
