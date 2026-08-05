"""Conversation-level orchestrator that survives WebSocket reconnects.

A ``Supervisor`` owns the long-lived resources that must persist across
resumptions: the ``ToolDispatcher``, ``TurnState``, and media objects. It builds
a new ``LiveSession`` for each WebSocket epoch and handles resumption handles,
GoAway, failure budgets, and in-flight tool results.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from google.genai import types

import tools._state as tools_state
from audit.journal import Journal
from core.commands import AgentCommand, Disconnect
from core.dispatcher import ToolDispatcher
from core.events import (
    AudioLevel,
    ConnectionState,
    ConnectionStateChanged,
    ContextReset,
    DisconnectReason,
    RateLimitHit,
    SessionError,
    UsageUpdate,
)
from core.session import LiveSession
from core.tier_manager import TierConfig, TierManager
from core.turn_state import TurnState
from media import devices
from media.microphone import Microphone
from media.screen import ScreenCapture
from media.speaker import Speaker
from obs.logging_setup import get_logger
from persist.store import Store
from policy import PolicyEngine
from settings import secrets
from settings.settings import AppSettings
from tools.registry import ToolResult

logger = get_logger(__name__)

_FAILURE_WINDOW = timedelta(minutes=5)
_HANDLE_TTL_MINUTES = 110
_MAX_BACKOFF_TRANSIENT = 30.0
_MAX_BACKOFF_QUOTA = 120.0
_METER_INTERVAL_SECONDS = 0.033
_BUDGETS: dict[str, int] = {
    "transient": 6,
    "rejected_handle": 6,
    "quota": 3,
}

# Rate-limit fallback ladder: once every (tier, model) pool is exhausted, back
# off 15s -> 30s -> 60s -> 300s (plus jitter, capped at the last rung) and
# emit a countdown tick every few seconds so the GUI can show the wait.
_RATE_LIMIT_BACKOFF_LADDER = (15.0, 30.0, 60.0, 300.0)
_BACKOFF_TICK_SECONDS = 5.0


class _EventTap:
    """Event-queue shim placed between a ``LiveSession`` and the real queue.

    Every event is forwarded unchanged (``RateLimitHit`` is first enriched
    with the supervisor's tier tracking, since the session does not know which
    key it is running on), while ``UsageUpdate``/``RateLimitHit`` metadata is
    captured for TPM headroom tracking and the fallback cascade.
    """

    def __init__(
        self,
        inner: asyncio.Queue[Any],
        on_event: Callable[[Any], Any],
    ) -> None:
        self._inner = inner
        self._on_event = on_event

    async def put(self, event: Any) -> None:
        await self._inner.put(self._on_event(event))

    def put_nowait(self, event: Any) -> None:
        self._inner.put_nowait(self._on_event(event))


class Supervisor:
    """Reconnect-capable conversation manager for the Gemini Live API.

    Parameters
    ----------
    settings:
        Runtime configuration.
    command_queue:
        Queue of commands from the GUI/driver.
    event_queue:
        Queue of events published to the GUI/driver.
    journal:
        Audit journal.
    store:
        Persistent SQLite store for turns, tool calls, and resumption handles.
    run_id:
        Identifier for this run.
    tier_manager:
        Dual-tier key router. When omitted, one is built from the API keys in
        settings/secrets; when no keys are configured at all, the supervisor
        falls back to the legacy single-key behavior (no fallback cascade).
    """

    def __init__(
        self,
        settings: AppSettings,
        command_queue: asyncio.Queue[AgentCommand],
        event_queue: asyncio.Queue[Any],
        journal: Journal,
        store: Store,
        run_id: str,
        *,
        tier_manager: TierManager | None = None,
    ) -> None:
        self._settings = settings
        self._command_queue = command_queue
        self._event_queue = event_queue
        self._journal = journal
        self._store = store
        self._run_id = run_id

        self._epoch = 0
        self._resumption_handle: str | None = None
        self._pending_tool_results: list[tuple[str, str, ToolResult]] = []
        self._failure_log: dict[str, list[datetime]] = {}
        self._live_session: LiveSession | None = None

        # Rate-limit fallback cascade state (Module 1). ``_current_tier``/
        # ``_current_model`` identify the pool the next epoch connects with;
        # ``_cascade_anchor`` pins the pool where the current cascade started
        # so the fallback order stays stable across epochs.
        if tier_manager is None:
            tier_manager = self._default_tier_manager()
        self._current_tier: TierConfig | None = None
        if tier_manager is not None:
            self._current_tier = tier_manager.get_tier("tier1") or tier_manager.get_tier(
                "free"
            )
            if self._current_tier is None:
                tier_manager = None  # no keys configured: legacy single-key path
        self._tier_manager = tier_manager
        self._current_model = settings.model
        self._exhausted_pools: set[tuple[str, str]] = set()
        self._cascade_anchor: tuple[TierConfig, str] | None = None
        self._rate_limit_backoff_stage = 0
        self._rate_limit_hit: RateLimitHit | None = None
        self._last_usage_total = 0

        self._mic: Microphone | None = None
        self._speaker: Speaker | None = None
        self._screen: ScreenCapture | None = None
        self._media_started = False

        self._turn_state = TurnState()
        policy = PolicyEngine(
            allow_roots=[self._settings.workspace_root],
            deny_globs=[r"*\.git*", "__pycache__"],
            never_allow_regex=[
                r"AIza[0-9A-Za-z\-_]{35}",
                r"sk-[A-Za-z0-9]{20,}",
                r"ghp_[A-Za-z0-9]{36}",
            ],
        )
        self._dispatcher = ToolDispatcher(
            policy=policy,
            journal=self._journal,
            event_queue=self._event_queue,
            run_id=self._run_id,
            epoch=self._epoch,
            store=self._store,
        )

        # The tool implementations keep their own module-level policy as a
        # safety net. Point it at the *same* engine and the session workspace,
        # otherwise it evaluates against the process cwd captured at import
        # time (wrong root for every write) and never sees gating-mode changes.
        tools_state.set_policy(policy)
        tools_state.set_workspace(self._settings.workspace_root)

    async def run(self) -> DisconnectReason:
        """Run the main reconnect loop until the user disconnects or we fail."""
        self._resumption_handle = self._store.get_active_resumption_handle(self._run_id)
        self._dispatcher.set_respond_callback(self._respond_to_model)
        self._store.start_run(
            self._run_id,
            datetime.now(UTC).isoformat(),
            self._settings.model,
            self._settings.mode,
            str(self._settings.workspace_root),
        )

        meter_task: asyncio.Task[Any] | None = None
        try:
            await self._start_media()
            if self._mic is not None or self._speaker is not None:
                meter_task = asyncio.create_task(self._meter_loop())

            while True:
                self._dispatcher.set_epoch(self._epoch)
                self._rate_limit_hit = None
                self._last_usage_total = 0
                if self._tier_manager is not None and self._current_tier is not None:
                    # Point the session at the current pool's key and model.
                    # The override lives inside secrets.get_api_key, which the
                    # session already calls, so no session.py change is needed.
                    secrets.set_active_api_key(self._current_tier.api_key)
                    session_queue: Any = _EventTap(
                        self._event_queue, self._observe_session_event
                    )
                else:
                    session_queue = self._event_queue
                live_session = LiveSession(
                    settings=self._session_settings(),
                    command_queue=self._command_queue,
                    event_queue=session_queue,
                    journal=self._journal,
                    store=self._store,
                    run_id=self._run_id,
                    epoch=self._epoch,
                    resumption_handle=self._resumption_handle,
                    mic=self._mic,
                    speaker=self._speaker,
                    screen=self._screen,
                    dispatcher=self._dispatcher,
                    turn_state=self._turn_state,
                    on_resumption_handle=self._on_resumption_handle,
                    on_connected=self._on_connected,
                )
                self._live_session = live_session
                reason = await live_session.run()
                self._live_session = None

                if reason.code == "user_request":
                    return reason

                if reason.code in ("auth_error", "not_found"):
                    detail = reason.detail or f"{reason.code}"
                    await self._emit(
                        ConnectionStateChanged(ConnectionState.FAILED, detail=detail)
                    )
                    return reason

                category = self._classify(reason)

                if self._tier_manager is not None and (
                    category == "quota" or self._rate_limit_hit is not None
                ):
                    # Rate limited: run the tier/model fallback cascade instead
                    # of the generic failure-budget path.
                    self._dispatcher.deny_all_pending(reason="connection_lost")
                    outcome = await self._handle_rate_limit(reason)
                    if outcome is not None:
                        return outcome
                    self._epoch += 1
                    continue

                if category == "go_away":
                    await self._emit(
                        ConnectionStateChanged(
                            ConnectionState.DRAINING, detail=reason.detail
                        )
                    )
                    self._dispatcher.deny_all_pending(reason="connection_lost")
                elif category in ("transient", "rejected_handle", "api_close", "quota"):
                    self._dispatcher.deny_all_pending(reason="connection_lost")

                if category == "rejected_handle":
                    if self._resumption_handle is not None:
                        self._store.delete_resumption_handle(self._resumption_handle)
                    self._resumption_handle = None
                    await self._emit(
                        ContextReset(reason="resumption handle rejected")
                    )

                if not self._check_failure_budget(category):
                    msg = f"failure budget exceeded for {category}"
                    await self._emit(
                        ConnectionStateChanged(ConnectionState.FAILED, detail=msg)
                    )
                    return DisconnectReason("failed", msg)

                backoff = self._backoff(category)
                await self._emit(
                    ConnectionStateChanged(
                        ConnectionState.RECONNECTING, detail=reason.detail
                    )
                )
                if backoff > 0:
                    await asyncio.sleep(backoff)

                self._epoch += 1
        finally:
            self._live_session = None
            secrets.set_active_api_key(None)
            self._store.end_run(self._run_id)
            await self._emit(ConnectionStateChanged(ConnectionState.DISCONNECTED))
            if meter_task is not None:
                meter_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await meter_task
            await self._stop_media()

    async def _on_resumption_handle(self, handle: str | None, resumable: bool) -> None:
        """Persist or clear a server-provided resumption handle."""
        if handle is None:
            self._resumption_handle = None
            return
        if resumable:
            created_at = datetime.now(UTC).isoformat()
            expires_at = (
                datetime.now(UTC) + timedelta(minutes=_HANDLE_TTL_MINUTES)
            ).isoformat()
            # Delete first so re-saving an existing handle does not violate the
            # primary-key constraint if the server reuses a handle.
            self._store.delete_resumption_handle(handle)
            self._store.save_resumption_handle(
                handle, self._run_id, created_at, expires_at
            )
            self._resumption_handle = handle
        else:
            self._store.delete_resumption_handle(handle)
            self._resumption_handle = None

    async def _on_connected(self, live_session: LiveSession) -> None:
        """Seed context on a fresh connect and drain queued tool results."""
        # A successful connect proves the current pool works again: reset the
        # rate-limit cascade so a later 429 starts a fresh fallback sequence.
        self._rate_limit_backoff_stage = 0
        self._cascade_anchor = None
        self._exhausted_pools.clear()
        if self._resumption_handle is None:
            prior_turns = self._store.get_turns(self._run_id)
            if prior_turns:
                await self._emit(
                    ContextReset(reason="no resumption handle; context seeded from store")
                )
                await live_session.send_client_content(
                    turns=[
                        types.Content(
                            role=role,
                            parts=[types.Part(text=text)],
                        )
                        for role, text, _epoch in prior_turns
                    ],
                    turn_complete=True,
                )

        pending = self._pending_tool_results
        self._pending_tool_results = []
        for call_id, name, result in pending:
            await self._deliver_tool_result(live_session, call_id, name, result)

    async def _respond_to_model(
        self, call_id: str, name: str, result: ToolResult
    ) -> bool:
        """Deliver a completed tool result to the current live session."""
        session = self._live_session
        if session is None:
            self._pending_tool_results.append((call_id, name, result))
            return False

        try:
            await session.send_tool_response(call_id, name, result)
            return True
        except Exception as exc:
            logger.debug("send_tool_response failed for %s: %s", call_id, exc)
            try:
                await self._send_tool_result_as_note(session, call_id, name, result)
                return True
            except Exception as exc2:  # pragma: no cover - defensive
                logger.debug("tool result fallback note failed for %s: %s", call_id, exc2)
                self._pending_tool_results.append((call_id, name, result))
                return False

    async def _deliver_tool_result(
        self,
        live_session: LiveSession,
        call_id: str,
        name: str,
        result: ToolResult,
    ) -> None:
        """Deliver a queued tool result to a newly connected session."""
        try:
            await live_session.send_tool_response(call_id, name, result)
        except Exception as exc:
            logger.debug("queued tool result failed for %s: %s", call_id, exc)
            await self._send_tool_result_as_note(live_session, call_id, name, result)

    async def _send_tool_result_as_note(
        self,
        live_session: LiveSession,
        call_id: str,
        name: str,
        result: ToolResult,
    ) -> None:
        """Send a tool result as a client text turn when the fc_id is rejected."""
        text = f"System note: tool result for {name} ({call_id}): {result.message}"
        await live_session.send_client_content(
            turns=[
                types.Content(
                    role="user",
                    parts=[types.Part(text=text)],
                )
            ],
            turn_complete=True,
        )

    async def _start_media(self) -> None:
        """Create and start the mic, speaker, and screen if the config requires."""
        if self._media_started:
            return

        if (
            self._settings.mode != "text"
            and self._audio_input_available()
            and self._mic is None
        ):
            self._mic = Microphone(device=self._settings.input_device)
            await self._mic.start()

        if self._audio_output_available() and self._speaker is None:
            self._speaker = Speaker(device=self._settings.output_device)
            await self._speaker.start()

        if self._settings.share_screen and self._screen is None:
            self._screen = ScreenCapture(
                fps=self._settings.screen_fps,
                monitor=self._settings.screen_monitor,
            )
            await self._screen.start()

        self._media_started = True

    async def _stop_media(self) -> None:
        """Stop the media objects owned by the Supervisor."""
        if self._screen is not None:
            try:
                await self._screen.stop()
            except Exception as exc:
                logger.warning("Screen stop failed: %s", exc)
            self._screen = None

        if self._mic is not None:
            try:
                await self._mic.stop()
            except Exception as exc:
                logger.warning("Mic stop failed: %s", exc)
            self._mic = None

        if self._speaker is not None:
            try:
                await self._speaker.stop()
            except Exception as exc:
                logger.warning("Speaker stop failed: %s", exc)
            self._speaker = None

        self._media_started = False

    async def _meter_loop(self) -> None:
        """Poll media levels and emit ``AudioLevel`` events."""
        try:
            while True:
                if self._mic is not None:
                    await self._emit(
                        AudioLevel(source="mic", level=self._mic.level)
                    )
                if self._speaker is not None:
                    await self._emit(
                        AudioLevel(source="speaker", level=self._speaker.level)
                    )
                await asyncio.sleep(_METER_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            pass

    def _classify(self, reason: DisconnectReason) -> str:
        """Map a session disconnect reason to the supervisor category."""
        if reason.code in (
            "transient",
            "rejected_handle",
            "quota",
            "go_away",
            "api_close",
        ):
            return reason.code
        return "transient"

    def _default_tier_manager(self) -> TierManager | None:
        """Build a ``TierManager`` from the keys in settings/secrets."""
        tier1_key = (
            self._settings.api_key_tier1
            or self._settings.api_key
            or secrets.get_api_key()
        )
        free_key = self._settings.api_key_free or secrets.get_free_api_key()
        if not tier1_key and not free_key:
            return None
        return TierManager(tier1_key=tier1_key, free_key=free_key)

    def _session_settings(self) -> AppSettings:
        """Return the settings for the next epoch, with the current model applied."""
        if self._current_model == self._settings.model:
            return self._settings
        return self._settings.model_copy(update={"model": self._current_model})

    def _observe_session_event(self, event: Any) -> Any:
        """Tap callback: track usage/rate limits and enrich ``RateLimitHit``.

        Returns the event to forward to the real queue (a replaced copy for
        ``RateLimitHit`` so the GUI sees the tier the session was on).
        """
        if isinstance(event, RateLimitHit):
            tier = self._current_tier
            limits = (
                tier.limits.get(self._current_model) if tier is not None else None
            )
            enriched = replace(
                event,
                model=self._current_model,
                tier=tier.tier_name if tier is not None else event.tier,
                tpm_used=tier.tpm_used if tier is not None else event.tpm_used,
                tpm_limit=limits.tpm_limit if limits is not None else event.tpm_limit,
            )
            self._rate_limit_hit = enriched
            return enriched
        if isinstance(event, UsageUpdate):
            self._record_tier_usage(event)
        return event

    def _record_tier_usage(self, event: UsageUpdate) -> None:
        """Feed token usage into the TierManager's rolling TPM tracker.

        Live API ``usage_metadata`` totals are cumulative within a session, so
        only the delta since the previous event is recorded.
        """
        if self._tier_manager is None or self._current_tier is None:
            return
        delta = max(0, event.total_tokens - self._last_usage_total)
        self._last_usage_total = max(self._last_usage_total, event.total_tokens)
        if delta:
            self._tier_manager.record_usage(
                self._current_tier, self._current_model, delta
            )

    async def _handle_rate_limit(self, reason: DisconnectReason) -> DisconnectReason | None:
        """Run the tier/model fallback cascade after a rate-limit disconnect.

        Returns ``None`` when the reconnect loop should immediately start the
        next epoch on a fresh pool, or a ``DisconnectReason`` when the run
        should end (user disconnect during the backoff countdown).
        """
        if self._current_tier is None or self._tier_manager is None:
            return None  # pragma: no cover - guarded by the caller
        if self._cascade_anchor is None:
            self._cascade_anchor = (self._current_tier, self._current_model)
        self._exhausted_pools.add((self._current_tier.tier_name, self._current_model))

        next_pool = self._next_pool()
        if next_pool is not None:
            tier, model = next_pool
            if model != self._current_model:
                self._switch_model(model, tier)
            else:
                self._switch_tier(tier)
            await self._emit(
                ConnectionStateChanged(
                    ConnectionState.RECONNECTING,
                    detail=(
                        f"rate limited; retrying on {tier.tier_name} key "
                        f"with model {model}"
                    ),
                )
            )
            return None

        # All four pools (2 tiers x 2 models) are exhausted: back off, then
        # start the cascade over from the primary pool.
        await self._emit(
            ConnectionStateChanged(
                ConnectionState.RECONNECTING,
                detail="all API key/model pools rate limited",
            )
        )
        outcome = await self._rate_limit_backoff()
        self._exhausted_pools.clear()
        self._cascade_anchor = None
        self._reset_primary_pool()
        return outcome

    def _next_pool(self) -> tuple[TierConfig, str] | None:
        """Return the next (tier, model) pool in cascade order, or ``None``.

        Anchored at the pool where the cascade started: same model on the
        other tier, fallback model on the anchor tier, fallback model on the
        other tier. Pools already exhausted or without TPM headroom are
        skipped.
        """
        if self._cascade_anchor is None or self._tier_manager is None:
            return None  # pragma: no cover - guarded by the caller
        anchor_tier, anchor_model = self._cascade_anchor
        other_tier = self._other_tier(anchor_tier)
        fallback_model = self._settings.fallback_model

        candidates: list[tuple[TierConfig, str]] = []
        if other_tier is not None:
            candidates.append((other_tier, anchor_model))
        if fallback_model and fallback_model != anchor_model:
            candidates.append((anchor_tier, fallback_model))
            if other_tier is not None:
                candidates.append((other_tier, fallback_model))

        for tier, model in candidates:
            if (tier.tier_name, model) in self._exhausted_pools:
                continue
            if self._tier_manager.tpm_headroom(tier, model) <= 0:
                continue
            return tier, model
        return None

    def _other_tier(self, tier: TierConfig) -> TierConfig | None:
        """Return the opposite configured tier, or ``None`` if single-keyed."""
        if self._tier_manager is None:
            return None
        other_name = "free" if tier.tier_name == "tier1" else "tier1"
        return self._tier_manager.get_tier(other_name)

    def _switch_tier(self, tier: TierConfig) -> None:
        """Move the next epoch to ``tier``'s API key, keeping the current model.

        The rate-limited session has already terminated by the time the
        cascade runs (a 429 ends the WebSocket), so there is nothing left to
        disconnect; the run loop starts the new session with this tier's key
        applied through ``secrets.set_active_api_key``.
        """
        logger.info(
            "Rate limit: switching tier %s -> %s (model %s)",
            self._current_tier.tier_name if self._current_tier else "?",
            tier.tier_name,
            self._current_model,
        )
        self._current_tier = tier

    def _switch_model(self, model: str, tier: TierConfig) -> None:
        """Move the next epoch to the fallback ``model`` on ``tier``'s key."""
        logger.info(
            "Rate limit: switching model %s -> %s (tier %s)",
            self._current_model,
            model,
            tier.tier_name,
        )
        self._current_tier = tier
        self._current_model = model

    def _reset_primary_pool(self) -> None:
        """Return to the primary model and preferred tier after a full backoff."""
        if self._tier_manager is None:
            return
        self._current_model = self._settings.model
        self._current_tier = (
            self._tier_manager.get_tier("tier1")
            or self._tier_manager.get_tier("free")
            or self._current_tier
        )

    async def _rate_limit_backoff(self) -> DisconnectReason | None:
        """Back off after every tier/model pool is exhausted.

        Walks the 15s -> 30s -> 60s -> 300s ladder (capped at the last rung,
        plus up to 10% jitter), emitting a ``SessionError`` countdown every
        tick so the GUI can display the wait. A ``Disconnect`` command during
        the wait ends the run.
        """
        rung = min(self._rate_limit_backoff_stage, len(_RATE_LIMIT_BACKOFF_LADDER) - 1)
        self._rate_limit_backoff_stage += 1
        delay = _RATE_LIMIT_BACKOFF_LADDER[rung]
        delay += random.uniform(0.0, delay * 0.1)
        if self._rate_limit_hit is not None:
            delay = max(delay, self._rate_limit_hit.cooldown_seconds)

        remaining = delay
        while remaining > 0:
            await self._emit(
                SessionError(
                    message=(
                        "All API keys are rate limited; "
                        f"retrying in {max(1, int(remaining))}s..."
                    ),
                    fatal=False,
                )
            )
            if isinstance(self._poll_command(), Disconnect):
                return DisconnectReason(
                    "user_request", "disconnect during rate-limit backoff"
                )
            tick = min(_BACKOFF_TICK_SECONDS, remaining)
            await asyncio.sleep(tick)
            remaining -= tick
        return None

    def _poll_command(self) -> AgentCommand | None:
        """Non-blocking command check; non-Disconnect commands are re-queued."""
        try:
            cmd = self._command_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        if not isinstance(cmd, Disconnect):
            # Leave it for the next session's send loop. Under contention this
            # can reorder the queue; acceptable during a rate-limit backoff.
            self._command_queue.put_nowait(cmd)
            return None
        return cmd

    def _record_failure(self, category: str) -> None:
        """Append a timestamped failure for ``category``."""
        now = datetime.now(UTC)
        window = [
            t
            for t in self._failure_log.get(category, [])
            if now - t < _FAILURE_WINDOW
        ]
        window.append(now)
        self._failure_log[category] = window

    def _check_failure_budget(self, category: str) -> bool:
        """Return ``True`` if the category is still within its failure budget."""
        self._record_failure(category)
        budget = _BUDGETS.get(category)
        if budget is None:
            return True
        return len(self._failure_log[category]) <= budget

    def _backoff(self, category: str) -> float:
        """Return the randomized backoff for the next reconnect attempt."""
        attempt = max(0, len(self._failure_log.get(category, [])) - 1)
        if category in ("transient", "go_away", "api_close", "rejected_handle"):
            return random.uniform(0, min(_MAX_BACKOFF_TRANSIENT, 0.5 * (2 ** attempt)))
        if category == "quota":
            return random.uniform(0, min(_MAX_BACKOFF_QUOTA, 5.0 * (2 ** attempt)))
        return 0.0

    def _audio_input_available(self) -> bool:
        try:
            return len(devices.list_input_devices()) > 0
        except Exception:
            return False

    def _audio_output_available(self) -> bool:
        try:
            return len(devices.list_output_devices()) > 0
        except Exception:
            return False

    async def _emit(self, event: Any) -> None:
        await self._event_queue.put(event)
