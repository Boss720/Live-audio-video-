# Handoff & Continuation Plan

## CURRENT — Review roadmap, 24 October 2026 (read this first)

- Plan: top section of `PLAN.md` ("Review roadmap"). Findings with file:line:
  `output/review-2026-10-24/live-agent-review.html` (untracked; `output/` is not committed).
- Branch `Leroy`. Waves 1-3 of earlier work committed as `1490dd7` (406 passed, 3 skipped).
- Wave 1 DONE and committed per module (M1 defaults, M2 privacy routing, M3 session
  policy + wake gate, M5 turn order). Gates: 439 passed, 3 skipped; ruff clean.
- mypy: `mypy .` aborts on a duplicate module name (scripts/reasoning_eval.py) — not a
  type error. Use the two scopes instead:
  `.venv/Scripts/python.exe -m mypy app audit core media obs persist policy settings tools main.py headless.py bridge_stdio.py` (40 files)
  and `.venv/Scripts/python.exe -m mypy scripts/eval_scoring.py scripts/reasoning_eval.py scripts/vision_eval.py` (3 files).
- Test command: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=<temp dir>`
  (default temp dir is sandbox-blocked). Ruff: `.venv/Scripts/python.exe -m ruff check .`
  (project does not use ruff format; do not reformat files).
- Commit rule: one commit per module, message via file, no Claude trailer.
- Behaviour changes in Wave 1: fresh installs default yolo=False, computer_use=False,
  model gemini-3.8-live. Saved user values still win. Strict privacy + Free-only key now
  fails before connect. prefer_free_tier=True (existing default) now really starts on
  Free when both keys exist — user confirmed 2026-10-24: keep Free first (no change).
- M3 needs live-socket checks: late ASR after tool response clears the addressed
  continuation; 300 ms ASR wait (`WAKE_TRANSCRIPTION_WAIT_SECONDS`) may be short; server
  must accept the "ignored" FunctionResponse for NON_BLOCKING tools without a call loop.
- Wave 2 latency DONE, 4 commits: `5522e64` store, `cb2324b` media+settings, `634a8eb` session,
  `d9eafc8` supervisor. Gates: 471 passed, 3 skipped; mypy both scopes clean; ruff clean.
  Store smoke: 200 writes enqueue 0.3 ms, flush+read 7.5 ms, journal_mode=wal.
- Wave 2 behaviour changes: Store writes are fire-and-forget on a "store-writer" thread (errors
  logged, not raised; reads flush first; close() drains). Speaker adds 40 ms prebuffer after
  start/flush/underrun. trigger_tokens=100000 by default. Stable 60 s clears the whole supervisor
  failure log (all categories). run_command/write_file/delete_file also INTERRUPT on extended thinking.
- Usage contract DECIDED (unverified live): usage_metadata per message = per-turn increment.
  If a live session shows totals growing cumulatively, revert supervisor `_record_tier_usage` to diffing.
- Live checks pending: M3 items above + Wave 2 items listed in PLAN.md.
- Wave 3 DONE, 6 commits: `0f9ed8c` echo gate, `1bf366c` frames, `af5372e` shell+redaction,
  `9d550e4` tools+edit_file, `c6ad674` computer/headless/wake, `25e9cd7` genai 2.33.0 (venv updated
  via `uv pip install`; venv has no pip). Gates: 561 passed, 3 skipped; mypy clean; ruff clean.
- Wave 3 behaviour changes: mic sends silence while the speaker is audible on non-headset output
  (no voice barge-in there; `echo_gate=off` restores it). Frames ~169 KB at q82/4:4:4. Store-style
  redaction may hit prose like `password: required`. Headless exits on stdin EOF. start_line=0 now
  fails validation. Wake word is looser for long names (Soundex within edit distance 2).
- Live checks pending: M3, Wave 2 and Wave 3 items in PLAN.md.
- Wave 4 UI DONE, 6 commits: `cb3c5b4` status strip, `d6656ef` chat view, `cb37d7c` approvals rail,
  `68bd53b` settings dialog, `291e0c8` diagnostics page, `8f816e6` main window wiring + table theme.
  Gates: 624 passed, 3 skipped; mypy and ruff clean. GUI launched (`pythonw main.py --no-headless`):
  status strip, Ctrl+, settings (risky box), Ctrl+Shift+D diagnostics checked by eye; not connected live.
- Wave 4 behaviour changes: inline settings grid is gone (Settings button / Ctrl+,). Live-applied on save
  while connected: yolo (SetGatingMode), share_screen + screen_monitor (SetShareScreen), minimize_to_tray.
  Everything else says "N changes apply on reconnect". Approvals rail shows Cancel only after Approve.
- Wave 4 gaps: DisconnectReason is not in the AgentEvent union, so the diagnostics Reconnects tile stays 0
  until the session emits it. Strip tier label is inferred from privacy_mode/prefer_free_tier until a
  RateLimitHit arrives. No Interrupt button (no command exists). Mock B (mini pill) not built.
- Launch note: AppSettings.headless defaults True, so plain `main.py` runs the terminal mode; use
  `--no-headless` for the GUI. The venv's python is 3.11 (uv trampoline), not 3.14.
- LIVE RESULTS 24 Oct 2026 (gemini-3.8-live, Free tier, speakers, computer_use + yolo on):
  - Usage contract CONFIRMED: usage_metadata per message = that turn's full context + reply (prompt
    4226/4269/4313 over 3 turns, total never accumulates). Summing for cost is right; UsageUpdate.prompt_tokens
    is the context size (diagnostics "Context" tile correct). Cost over-estimates if context is cached.
  - Replay with turn_complete=False: silent (0 audio bytes in 6 s). OK.
  - Tool round trip on gemini-3.8-live with WHEN_IDLE: accepted, answer spoken after, no call loop. OK.
  - UNVERIFIED: INTERRUPT on gemini-3.8-live-extended-thinking. Model says "let me check" but emits no
    function call in 30 s (4 tries, with and without prior turns). Dropping NON_BLOCKING is rejected by the
    server (1007 "BLOCKING function calls are not supported for this model"). Test again in the GUI.
  - Voice session (8 turns): speech_end_to_first_audio p50 609 ms, p95 735 ms. Echo gate works: 1036
    chunks x 40 ms = 41.4 s suppressed = speaker.played_ms 41.8 s. Context 15.9k. No reconnects.
  - FOUND+FIXED: speaker.underruns counted every idle/prebuffer callback (12657). Now counts only
    callbacks that were playing and ran dry (one per utterance end is expected). Test added.
  - Frames at 1568 px (computer_use): ~294 KB avg (9.4 MB / 32), capture 78 ms. Larger than the 169 KB
    estimate at 1280 px. screen_deduplicate is off by default so "skipped" shows a dash.
  - Watch: mic.dropped_chunks 37 of 7335 (0.5%, 1.5 s of capture older than 400 ms, likely at connect).
    speaker.high_water 11.9 s: server sends audio far faster than real time, so flush on barge-in matters.
  - Log scan of the 19:19 session (agent.log): no ERROR/WARNING. Wake gate: 176 ADDRESSED vs 232 GATED in
    the last 3000 lines; gated tool calls (list_directory, computer_mouse_move) answered "ignored" with no
    loop. Barge-in flush fires on every gated utterance. GUI closed after the session.
  - Not yet checked live: barge-in on speakers (expected disabled), wake-word false positives, click
    accuracy at 1568 px, headless EOF, GoAway resume, late-ASR wake wait (M3).
- 25 Oct 2026: DisconnectReason emitted (`7a63330`), companion pill + mini mode (`80220ad`, `fbeb15b`).
- Wave 5 (spec `docs/wave5.md`, tracked in PLAN.md). Trinity toolkit installed (`95ae87a`): Codex and
  Antigravity run as workers. Caveats: `trinity dispatch` defaults `-Base main` (initial commit here), so
  always pass `-Base Leroy`; `trinity merge` refuses unless the main checkout is on `main`, so worker
  branches are cherry-picked onto Leroy after review + full tests. AGENTS.md has the project commands.
  - W5.1 Interrupt `409b086`: Esc flushes playback and drops the rest of the current reply (also before
    its first audio). Codex review caught the early-Esc gap; fixed and tested.
  - W5.3 wake_review `b2a6693`: `python scripts/wake_review.py --last`. Found "Roy" heard for "Leroy"
    on gated turns. Cheapest fix: add "Roy" to wake_word_aliases (user setting, not changed).
  - W5.2 frame budget `248f040`; W5.4 pill paint + Stop/End `7846821`. Gates: 661 passed, 3 skipped;
    ruff, mypy clean. GUI launched: Mini mode pill renders, Stop greyed while disconnected. Not connected live.
  - Stray empty `None` file appeared twice in the repo root (not from pytest or the render script;
    likely trinity tooling). Moved to scratchpad. Watch for it.
- NEXT: live voice session: Esc mid-reply, pill Stop/Hold/approvals, frame quality at 1568 px for
  click accuracy. Consider alias "Roy" for wake word "Leroy".

## Previous handoff — 24 October 2026 (historical)

This section supersedes historical state and counts below. Read this first after
a context/usage reset. User authorized beginning the assessment roadmap and asked
for a handoff before usage runs out. Initial handoff saved before implementation.

- Workspace: `C:\Users\Hp\Dev\Projects\gemini-live-agents\gemini-live-agent`
  (Windows junction may resolve to `D:\AI\projects\Dev\Projects\gemini-live-agents\gemini-live-agent`).
- Branch: `Leroy`; baseline HEAD `dcd709786c768cf5c4c02aa90486733fa8996e99`.
- Origin: `https://github.com/Boss720/Live-audio-video-.git`.
- Before implementation, dirty files were the preceding assessment's
  `PROGRESS.md` and `output/assessment-2026-10-24/` artifacts. Preserve them.
- First correctness batch implemented and independently verified: newest
  conversation restoration, failed result replay retention, ordered desktop
  inputs, timer-driven transcript delivery, and nonblocking screen shutdown.
- Latest verified source checkpoint: **406 passed, 3 skipped in 22.51s**;
  runtime mypy 43 files and evaluation mypy 3 files passed; full Ruff clean.
  Waves 2 and 3 completed: audio/timing diagnostics, configurable mic chunks
  (100ms default retained), batched usage persistence, repaired scorers, screenshot
  freshness/optional exact dedup, tool schema constraints, compact settings and
  approval/diagnostics QoL. Saved preferences survive restart; explicit CLI flags
  override them. Failed screen preflight returns unsent text without overwriting
  a newer draft. All source edits remain uncommitted.
- Final source logs: `output/implementation-wave3-2026-10-24/pytest-full.log`,
  `mypy.log`, `mypy-eval.log`, `ruff.log`. Synthetic GUI screenshots at 720x520
  and 1100x800 are alongside them. Earlier checkpoint counts below are historical.
- FINAL PACKAGING OUTCOME (supersedes investigation notes below): verified
  `dist/improvements-2026-10-24-final/gemini-live-agent/gemini-live-agent.exe`.
  Production build and --help passed. Native disconnected window inspected,
  raised, captured as `candidate-native-final.png` with AX state alongside it,
  then closed via Ctrl+Q; PID45108 absence confirmed. No provider/media session.
  Coordinator independently reran the package audit:40/40 own modules match
  source, all15 critical present, no forbidden entries. Bundle236 files,
  162,829,998 bytes. EXE SHA256:
  `b1b03f3d7ce3965eea00ab8d2eaf15a3cee1659a41b113e7c401bec501f0d4cb`.
  No diagnostic hook embedded. `package-audit.json` is the FINAL manifest;
  `package-audit-initial.json` preserves the earlier flagged candidate.
- Portable ZIP verified:
  `dist/improvements-2026-10-24-final/gemini-live-agent-improvements-2026-10-24.zip`
  (72,201,788 bytes); CRC and all236 bundle members match manifest size/SHA256.
  ZIP SHA256: `3080339ad10a671906ab2e6e08eabb4f1416e5483e6cdfde776914ed4e09dc2a`.
  See `portable-audit.json`. Extract the entire archive; keep EXE and `_internal`
  together. Unreleased local candidate, existing0.3.0 version resources retained.
  All test/diagnostic processes stopped. No active build/check remains.
- Historical packaging investigation (completed): first candidate built at
  `dist/improvements-2026-10-24/gemini-live-agent/gemini-live-agent.exe`, using
  existing `packaging/gemini-live-agent.spec`, separate work/cache directories.
  Published bundles are preserved; no installer/publication/version bump requested.
  Build/help pass; 40/40 own modules match source (15 critical), 236 files /
  162,835,996 bytes. Initial audit flags three `_pytest` support modules; spec
  now excludes `_pytest`; final rebuild/audit passed as recorded above.
  Initial sandbox GUI smoke PID43792 produced no visible window; coordinator
  stopped that exact test process after verifying its path. No provider connection.
  Source native/offscreen startup under isolated profile succeeds in ~5s.
  Temporary diagnostic build completed via exec session35806, using
  `output/implementation-wave3-2026-10-24/diagnostic.spec` and trace hook. It is
  not a deliverable. Expected diagnostic exe:
  `dist/improvements-2026-10-24-diagnostic/gemini-live-agent/gemini-live-agent.exe`.
  Trace reached every stage including `show: done`; there was no constructor
  hang. The sandbox's window was not on the normal desktop. An authorized
  normal-filesystem launch of the original candidate PID43804 showed the native
  window via CUA (ID4654522). Raised, visually inspected and saved screenshot
  `candidate-native-initial.png`; closed with Ctrl+Q and process absence checked.
  Temporary diagnostic PID48432 was also stopped after exact path verification.
  Final build completed via exec session15519 using production spec WITHOUT hook,
  output `dist/improvements-2026-10-24-final/` and matching `build/` path.
  `package_audit.py --candidate <exe>` inventories all onedir files and checks40
  own modules; worker and coordinator reruns both passed.
- Isolated smoke launcher uses placeholder keys, text mode, disabled sharing/
  computer control, task-owned profile/config/log/temp/workspace. Platformdirs
  requires `WIN_PD_OVERRIDE_LOCAL_APPDATA` (APPDATA alone is insufficient).
  `candidate_smoke.py help` passed. Native inspection uses mcp__cua_repl;
  call `cua.rewriteDocumentation()` after compaction, inventory windows then
  getApp by observed ID. Separate node_repl Sky initialization failed due missing
  kernel assets, but CUA native inspection and screenshot capture work. Native
  test launches need `require_escalated` so windows reach the normal desktop.
- Final source gates remain406/3 with43+3 mypy and clean Ruff. Only subsequent
  production edit is `_pytest` exclusion in packaging spec. Final package
  manifest at `output/implementation-wave3-2026-10-24/package-audit.json`.
  Usage checkpoint: five-hour83% used (17% remaining), weekly26% used. Handoff
  saved before limit as requested; no resets available. Packaging/docs finished.
- No commit/publication or live Gemini connection/media streams were performed.
  Native candidate GUI startup used isolated config/dummy keys; no Connect action.
- Assessment/roadmap: `output/assessment-2026-10-24/ASSESSMENT.md`.
  Raw evidence and mockup verification are alongside it. Three mock UI concepts
  exist; UI implementation is deferred until the core batch and measurement.
  Workbench is the proposed default; a carousel view alone is not a decision.
- Source of inline UI concepts:
  `C:\Users\Hp\.codex\visualizations\2026\10\23\01a16b25-4572-7e93-87c3-59119bf03d1c\gemini-live-ui-alternatives.html`.
- Baseline checks: 277 passed, 3 skipped; mypy runtime scope passed (42 files);
  Ruff has one existing E501 at `settings/settings.py:71`. Synthetic diagnostics
  reproduce bugs; do not interpret their success as fixed behavior.
- Use `.venv\Scripts\python.exe`, not a possibly stale `uv run` trampoline.
  Sandbox pytest temp setup failed due Windows permissions; normal-filesystem
  full-suite rerun passed. Use a fresh workspace-owned `--basetemp` for each run,
  and request tool escalation when necessary. Avoid recursively removing temps.
- Final implementation checks: **297 passed, 3 skipped in 16.99s**; runtime mypy
  passed across 42 source files. Ruff retains only baseline E501 at
  `settings/settings.py:71`; no new findings. `git diff --check` passed.
  Commands, regression excerpts and limitations:
  `output/implementation-2026-10-24/VERIFICATION.md`; raw full logs alongside it.
- Changed runtime files: `persist/store.py`, `core/supervisor.py`,
  `core/dispatcher.py`, `core/turn_state.py`, `core/session.py`, `media/screen.py`.
  Modified tests: `test_store_p3.py`, `test_supervisor.py`, `test_dispatcher.py`,
  `test_turn_state.py`; new `test_screen_shutdown.py`, `test_transcript_delivery.py`.
  Tests live under `tests/`. All implementation changes remain uncommitted.
- New tests protect recent/equal-timestamp history, partial replay failure,
  FIFO action/frame order, queued cancellation/denial/shutdown, single-packet
  caption delivery, wake gating, coalescing/final order, capture stop/restart, and
  delayed monitor validation preserving the shutdown signal after capture stops.
- Next entry: inspect current branch/status first, then proceed with representative
  live evaluation or the remaining roadmap as authorized. Source and local
  candidate verification are complete. All worker source edits have finished.
  The old assessment `diagnostic_probes.py` deliberately asserts buggy baseline
  behavior and must not be used as a current passing gate.
- Deferred from this narrow recovery fix: durable result outbox and compact
  completed-tool/current-task checkpoint. Latest recent-turn reconstruction is
  fixed, but neither restart-safe replay nor exactly-once delivery is promised.
- Remaining roadmap: durable replay/task checkpoints, background persistence/log
  consumers, a measured playback overload policy, representative end-to-end
  provider/device evaluation, session history browser and optional full UI redesign.
  Native target-focus recovery also remains. No hardware speedup/provider accuracy
  gain should be claimed without separate live evidence.
- Synchronous desktop input already running cannot be aborted; shutdown awaits
  it before freeing the desktop lane. Native capture that does not stop retains
  ownership and blocks restart on the same instance. No real-device behavior or
  package rebuild was validated during this batch.
- Latest usage read before dispatch: five-hour window 44% remaining; weekly 91%
  remaining. These values are transient; query fresh limits if needed.
- Final review found another existing monitor-switch/stop race. A failing fake
  validation regression was captured, the sentinel is now preserved, and full
  gates were rerun after that final source change. Exact final logs:
  `pytest-full-reviewed.log`, `mypy-reviewed.log`, `ruff-changed.log` under
  `output/implementation-2026-10-24/`. Baseline full Ruff evidence: `ruff.log`.

## Historical handoff (older state; retained for provenance)

## Current State

- **Repository:** `d:/AI/projects/Dev/Projects/gemini-live-agents/gemini-live-agent`
- **Remote:** `https://github.com/bosscube1/Audio-video-agent-harness.git`
- **Branch:** `Leroy` (HEAD: `5f7eb91`)
- **Latest milestone:** Complete Codebase Assessment & Improvement Roadmap (2026-10-05)
- **Test suite:** 260 passed, 3 skipped; strict `mypy` and `ruff` clean across all files.
- **Completed in this sprint:**
  - Milestone 1: GUI settings preservation on Save/Connect (`_build_settings`), strict privacy routing, and full 30 voice synchronization (`app/main_window.py`, `settings/settings.py`, `main.py`).
  - Milestone 2: Shell exit code correctness, Windows subprocess tree termination (`taskkill`), workspace root revocation on switch, symlink escape prevention, secret redaction, and bounded chronological tool history (`tools/shell.py`, `tools/_state.py`, `policy/engine.py`, `policy/redaction.py`, `persist/store.py`, `core/dispatcher.py`, `audit/journal.py`).
  - Milestone 3: Discrete user turn completion & non-accumulating transcripts, source-qualified turn IDs, wake-gated tool suppression on unaddressed voice responses, serialized screen frame capture with `_frame_lock`, per-modality cost attribution, and Free tier usage `estimated_usd=None` (`core/turn_state.py`, `core/session.py`, `core/supervisor.py`).
  - Milestone 4: Stdio bridge clean EOF handling, boolean string coercion, and robust mode parsing (`bridge_stdio.py`, `tests/test_bridge_stdio.py`).
  - Milestone 5: Executable packaging & Windows installer generation (`packaging/gemini-live-agent.spec`, `packaging/installer.iss`, `dist/gemini-live-agent/gemini-live-agent.exe`, `dist/installer/gemini-live-agent-setup-0.2.0.exe`).
  - Regression Test Suites: `tests/test_lane2_tools_policy_persist.py` and `tests/test_session_wake_tool.py` passing cleanly without resource warnings.
- **Status:** All core modules, sessions, tools, policies, persistence, GUI, bridges, onedir bundle, and Windows installer fully built and verified.

## Dual-tier optimization (2026-08-05, Modules 0–3 of 5)

Driven by `PLAN.md` + the rate-limit report (both in repo root): the operator runs
two Gemini API keys — Tier 1 paid and Free — and the app routes/fails over
between 4 Live API pools (2 tiers × 2 models). Rate limits (verified from the
AI Studio dashboard, stored in `core/tier_manager.py`):

| Model | Tier 1 TPM | Free TPM | RPM/RPD |
|:------|-----------:|---------:|:--------|
| `gemini-2.5-flash-native-audio-preview-12-2025` | 1,000,000 | 1,000,000 | unlimited |
| `gemini-3.1-flash-live-preview` (default) | 150,000 | 65,000 | unlimited |
| `gemini-3.5-live-translate-preview` | 100,000 | 20,000 | unlimited |

### Module 0 — foundation

- **`core/tier_manager.py` (new):** `ModelTierLimits`, `TierConfig`, `TierManager`
  with `select_tier(needs_privacy, has_video, model)`, a 60 s sliding-window TPM
  tracker (`record_usage` / `tpm_headroom` / `reset_if_needed`), injectable clock.
  Routing: privacy → Tier 1 (raises `ValueError` if no Tier 1 key — never silently
  downgrades); video → most headroom (Tier 1 wins ties); audio-only → Free first.
- **`settings/settings.py`:** `api_key_tier1`, `api_key_free` (legacy `api_key` =
  fallback for tier1), `privacy_mode`, `prefer_free_tier`, `target_tokens` 24000
  (now actually wired into `as_connect_config`), `target_tokens_tier1` 32000,
  `media_resolution`, `enable_affective_dialog`, `thinking_level`,
  `google_search_enabled`. All three API keys are excluded from `settings.json`.
  Enums follow the existing tuple+`field_validator` pattern (no `Literal`).
- Tests: `tests/test_tier_manager.py` (12).

### Module 1 — 429 handling & fallback cascade

- **`core/events.py`:** `RateLimitHit(model, tier, tpm_used, tpm_limit,
  cooldown_seconds)` — the session emits it with empty tier/zeros; the supervisor
  enriches it (pinned contract between the two).
- **`core/session.py`:** `_is_rate_limit` covers both real SDK shapes (google-genai
  2.16.0): in-band `ClientError` (`status=="RESOURCE_EXHAUSTED"` / code 429) and
  WebSocket closes where only the reason string carries `RESOURCE_EXHAUSTED`/`429`/
  `quota`. Exactly one event per session (`_rate_limit_emitted` guard), hooked into
  all three error paths. `generation_complete` without `turn_complete` is promoted
  via `model_copy(update={"turn_complete": True})` through the identical
  turn-boundary path.
- **`core/supervisor.py`:** optional `tier_manager` kwarg (no keys → legacy quota
  path unchanged). `_EventTap` shim between session and event queue: enriches
  `RateLimitHit`, records `UsageUpdate` deltas into the TPM tracker (**assumes
  Live `usage_metadata` is cumulative per session — deltas would undercount if a
  model ever reports per-turn increments**). Cascade anchored at first-429 pool:
  same model/other tier → fallback model/same tier → fallback model/other tier →
  backoff 15→30→60→300 s (+≤10 % jitter, honors `cooldown_seconds`), `SessionError`
  countdown every 5 s, stays responsive to `Disconnect`. Success resets stage.
  Backoff delays are module constants — tests patch them to ~0.
- **Key switching without touching session.py:** `secrets.set_active_api_key()`
  process-local override (consulted first by `get_api_key()`); model switch via
  `settings.model_copy`. **`settings/secrets.py`:** `get/set_free_api_key()`
  (`GOOGLE_API_KEY_FREE` env → keyring username `api_key_free`).
- **`main.py`:** `_with_tier_keys()` resolves both keys into settings at startup
  (`main.py` never constructs the Supervisor — `headless.py`/`app/bridge.py` do;
  the Supervisor default-constructs its TierManager from settings+secrets, so both
  entry points are covered).
- Resumption handles survive pool switches (only cleared on `rejected_handle`).
- Tests: `tests/test_session_rate_limit.py` (4), `tests/test_supervisor_fallback.py`
  (4). `tests/fakes/fake_live.py` gained scripted receive errors +
  `complete_generation()`.

### Module 2 — VAD & audio

- `vad_prefix_padding_ms` 300→**30**, `vad_silence_duration_ms` 800→**500**
  (already settings fields; only defaults changed).
- `audio_stream_end=True` sent inline on mic-gate close (send failures logged,
  never kill the send loop) and exactly once on teardown if audio streamed
  (`_audio_streamed` flag). **SDK constraint: `send_realtime_input` raises unless
  exactly one kwarg is set — `audio_stream_end` must be sent alone.**
- `UsageUpdate.estimated_usd` is now `float | None` (None = free/unknown → GUI
  should show "Free"). Pricing table `_COST_PER_MILLION_TOKENS` at
  `core/session.py:71` ($0.15 in / $0.60 out per 1M, prefix-matched).
  **`LiveSession` always computes the paid estimate — nulling it for free-tier
  sessions was implemented and verified in the supervisor `_EventTap` (`core/supervisor.py:548`).**
- Tests: `tests/test_session_vad_cost.py` (6).

### Module 3 — tools & API features (heavily corrected against the live API)

The plan's API names were partly invented; everything below is what google-genai
2.16.0 + the live backend actually support:

- **Scheduling:** no `ResponseSchedule`/`ScheduleType`/`ScheduledResponse` exists.
  Real mechanism = `FunctionDeclaration.behavior` (`BLOCKING`/`NON_BLOCKING`) +
  `FunctionResponse.scheduling` (`WHEN_IDLE`/`SILENT`/`INTERRUPT`).
  `tools/registry.py`: `FUNCTION_SCHEDULES` (plain strings), `schedule_enums()`,
  `tool_schedules()`, `build_tools(settings)` (new entry point; legacy
  `build_function_declarations` preserved — the fallback shim in
  `core/session.py::_build_tools` can be removed once `build_tools` is trusted).
- **All function declarations consolidated into ONE `types.Tool`**; per-declaration
  `behavior` stamped from `FUNCTION_SCHEDULES`; responses carry `scheduling` for
  non-blocking tools.
- **Google Search:** `types.Tool(google_search=types.GoogleSearch())` when
  `google_search_enabled` — **but only on models that accept it** (see live
  findings). No `web_search` tool exists anymore (Search grounding replaces it).
- **Code execution:** `types.ToolCodeExecution` wired but gated behind
  `CODE_EXECUTION_ENABLED = False` in `tools/registry.py` (no settings field).
- **`LiveConnectConfig` fields set in `core/session.py::_session_config_fields`:**
  `media_resolution`, `enable_affective_dialog` (model-gated),
  `thinking_config=ThinkingConfig(thinking_level=...)`. **`safety_settings` is
  deliberately NOT sent** (live findings).
- Tests: `tests/test_tool_registry.py` (10), `tests/test_session_config.py` (14).

### Live-API findings (verified by direct probes 2026-08-05 — trust these over PLAN.md)

1. **`safetySettings` is rejected by the Live backend** (`1007 Unknown name
   "safetySettings" at 'setup'`) even though the SDK serializes it. Omitted.
2. **`gemini-2.5-flash-native-audio` (bare id) does not exist** on v1beta
   bidiGenerateContent (`1008 not found`). The working id is
   **`gemini-2.5-flash-native-audio-preview-12-2025`** — now the `fallback_model`
   default and the TierManager limits key. Other dead ids probed:
   `gemini-2.5-flash-preview-native-audio-dialog`,
   `gemini-2.5-flash-exp-native-audio-thinking-dialog`,
   `gemini-live-2.5-flash-preview`, `gemini-2.0-flash-live-001`.
3. **`google_search` and `enable_affective_dialog` are model-dependent:** rejected
   with a misleading `1011 quota/internal error` on `gemini-3.1-flash-live-preview`,
   accepted on the 2.5 native-audio family. Gated by
   `core/session.py::_supports_search_and_affective()`.
4. **All 4 pools connect OK** with the full current config (verified after fixes).
5. The old finding stands: native `computer_use` tool dies with 1011 on first
   invocation — the nine `computer_*` function tools remain the mechanism.

### Field-testing fixes (2026-08-05, post-Module 3)

- **`computer_screenshot` moved `non_blocking_when_idle` → `blocking_interrupt`.**
  As non-blocking, the model announced actions while the screenshot was deferred
  until idle → "says it will do it, then nothing happens". Screenshot is on the
  critical path of every computer-use action; never defer it.
- **Defaults changed after A/B against pre-update behavior:** `thinking_level`
  `"minimal"` → **`"medium"`** (minimal caused failed multi-step tasks),
  `media_resolution` `"low"` → **`"high"`** (low caused screen misreads). Both are
  persisted in the user's `settings.json` too. Tier-aware defaults (low on Free)
  are a Module 4 item if TPM becomes a problem.
- YOLO approval path audited and confirmed intact (GUI → `SetGatingMode` →
  shared dispatcher; survives cascade reconnects). User confirmed approvals fine.

### Files touched (all in commit `b35ee20`)

Modified: `core/events.py`, `core/session.py`, `core/supervisor.py`, `main.py`,
`settings/__init__.py`, `settings/secrets.py`, `settings/settings.py`,
`tests/fakes/fake_live.py`, `tools/__init__.py`, `tools/registry.py`.
New: `core/tier_manager.py`, `tests/test_tier_manager.py`,
`tests/test_supervisor_fallback.py`, `tests/test_session_rate_limit.py`,
`tests/test_session_vad_cost.py`, `tests/test_session_config.py`,
`tests/test_tool_registry.py`.

### Operator configuration

- Keys live in Windows Credential Manager, service `GeminiLiveAgent`: username
  `api_key` (Tier 1 paid), `api_key_free` (Free). Env overrides: `GOOGLE_API_KEY`
  / `GOOGLE_API_KEY_FREE` (or `GEMINI_API_KEY_TIER1` / `GEMINI_API_KEY_FREE`).
  Set via `settings.secrets.set_api_key()` / `set_free_api_key()`.
  **The paid key has an atypical format (`AQ.Ab8...`) but is confirmed working.**
- GUI has no dual-key fields — keys are env/keyring only (Module 4 candidate).

## Remaining work (Modules 4–5 + deferred follow-ups)

### Module 4 — GUI integration (~3 h, depends on 0–3)

Per `PLAN.md`: tier indicator in status bar (green Tier 1 / yellow Free,
clickable), "Private Session" checkbox forcing Tier 1 (auto-on with Computer
Use/screen share), TPM usage meter (green <50 % / yellow 50–80 % / red >80 %),
per-session cost display on Tier 1 / "Free tier — no cost" on Free, cooldown
countdown during backoff, bridge wiring for `RateLimitHit`/tier-switch/
`SessionError` signals. **Also pick up these deferred items:**

- Null `UsageUpdate.estimated_usd` for free-tier sessions in the supervisor
  `_EventTap` (session can't know its tier).
- Tier-aware media resolution default (low on Free, high on Tier 1) — user
  testing showed `high` is needed for computer use; only drop to `low` when TPM
  pressure demands it.
- Optional: dual-key fields in the GUI settings dialog.

### Module 5 — polish (~1 h)

- Audit/trim the system instruction (~400 tok of duplicated tool descriptions).
- README: dual-tier setup, env vars, routing table.
- Remove the `build_function_declarations` fallback shim in
  `core/session.py::_build_tools` once `build_tools` is trusted.

### Other known loose ends

- `ProactivityConfig(proactive_audio)` exists in the SDK but no setting sources
  it — decide if wanted.
- `CODE_EXECUTION_ENABLED` flag exists but is off and untested live.
- 1011 errors from the Live API are ambiguous (quota vs unsupported feature vs
  transient) — when a new config field fails, bisect it the way
  `safetySettings`/`google_search` were diagnosed (bare config → add one field
  at a time).

## Computer use (mouse/keyboard control, 2026-08-02)

The agent can now drive the desktop directly instead of scripting everything
through PowerShell: move/click/double/right-click, type, key combos, drag,
scroll, and on-demand screenshots.

- **Not the native `computer_use` tool.** Probing (`scripts/probe_computer_use_live.py`)
  showed the Live API *accepts* `types.Tool(computer_use=...)` at setup but the
  backend dies with `1011 internal error` the moment the model invokes it
  (`gemini-3.1-flash-live-preview`; TEXT-modality models don't run on Live at
  all). So the actions are declared as nine ordinary function tools
  (`computer_mouse_move`, `computer_left_click`, `computer_right_click`,
  `computer_double_click`, `computer_type`, `computer_key`, `computer_drag`,
  `computer_scroll`, `computer_screenshot`) — same protocol shape, through the
  existing registry/dispatcher/policy path.
- **Executor:** `tools/computer.py` (pyautogui, new dependency). Tool
  coordinates are normalized 0–1000 on both axes of the shared monitor
  (`media.screen.monitor_geometry` maps them to physical pixels). pyautogui
  FAILSAFE is on: mouse slammed into a screen corner aborts the action.
  Non-ASCII text is pasted via clipboard (pyperclip), ASCII is typed.
- **Feedback loop:** Live tool responses can't carry images, so after every
  `computer_*` call the session grabs a one-shot JPEG (`media.screen.capture_jpeg`)
  and pushes it via `send_realtime_input(video=...)` — the model sees the
  result of each action immediately (`core/session.py::_push_fresh_frame`).
- **Gating:** all state-changing actions are in `_DESTRUCTIVE_TOOLS` → approval
  card in GUARDED mode; `computer_mouse_move` / `computer_screenshot` are
  auto-allowed. YOLO mode skips approvals as usual, so `yolo + computer_use`
  gives the agent full control without per-action confirmations.
- **Screen-share protection:** `SetShareScreen(enabled=False)` is ignored while
  `computer_use_enabled` is active, so the model cannot be blinded mid-task.
- **Flag:** `computer_use_enabled` (settings.json / `GEMINI_COMPUTER_USE_ENABLED`
  / GUI "Computer control" checkbox). Implies screen sharing — the GUI never
  sends `SetShareScreen(enabled=False)` when it's on, and the system prompt
  gains a "Computer Control" section explaining the 0–1000 grid.
- **Tests:** `tests/test_computer_tools.py` (30 tests: coordinate mapping,
  executor fakes, policy gating, registry declarations, dispatcher round-trip).
  Full suite 132 passed, ruff/mypy clean. Live-verified: model emits
  `computer_screenshot` correctly when asked; mss/pyautogui coordinate spaces
  match on the dev machine (1920×1080, no DPI offset). **Goal audit:** 133
  passed, ruff/mypy clean; YOLO full-control verified with expanded tests.

## /goal production fixes (2026-08-01, commit `c7d622d`)

Two production blockers reported against the installed build, traced to three defects:

1. **Yolo mode still prompted in the GUI.** `Bridge.send_command` silently
   returned while the QThread's asyncio loop didn't exist yet, dropping the
   `SetGatingMode(YOLO)` the GUI fires right after `start()`. Commands are now
   buffered under a lock and drained into the queue when `run()` starts.
   Headless was unaffected.
2. **File creation failed everywhere.** Tool implementations evaluated a
   module-level policy/workspace in `tools/_state.py` captured from process
   cwd at import time — never synced to the session workspace or gating mode.
   `Supervisor.__init__` now shares its `PolicyEngine` with `tools._state`
   (`set_policy` / `set_workspace`).
3. **Frozen installs couldn't write.** The installed exe's cwd is
   `C:\Program Files\GeminiLiveAgent`, which is also in the policy's
   `_immutable_dirs()` via `sys.executable.parent` — every write denied.
   `main.py` now defaults frozen apps to `~/Documents/GeminiLiveAgent`
   (`_default_workspace` / `_resolve_working_dir`) and redirects a persisted
   install-dir workspace from `settings.json` (`_guard_frozen_workspace`).

Regression tests: `tests/test_gui_bridge.py` (command buffering),
`tests/test_supervisor.py` (tools-state sync + yolo write without approval),
`tests/test_gui_lifecycle.py` (yolo write end-to-end), `tests/test_main_entry.py`
(6 tests for the frozen-workspace helpers). Full suite: 73 passed, ruff/mypy
clean. Rebuilt and silently reinstalled to `C:\Program Files\GeminiLiveAgent`.

## Phase 6 additions

- **Wheel fixed.** `pyproject.toml` hatch target replaced `packages = [".", ...]` with an explicit `include` list; verified `python -m build` produces a wheel whose install imports every package cleanly.
- **Run lifecycle persisted.** `Supervisor.run()` calls `store.start_run()` / `end_run()`; `start_run` is now idempotent (`INSERT OR IGNORE`).
- **Usage persisted.** `LiveSession` records per-modality usage rows. **Bug fix:** genai 2.x `UsageMetadata` has no `completion_token_count` / `audio_token_count` / `video_token_count` — completion now reads `response_token_count`, audio/video come from `prompt_tokens_details` / `response_tokens_details`. These were previously always 0.
- **New accessors:** `Store.get_usage(run_id)`, `Store.get_run(run_id)`.
- **PyInstaller onedir build.** `packaging/gemini-live-agent.spec` (+ generated `icon.ico`, `version.txt`). Hidden imports for `mss` and `_sounddevice_data` PortAudio DLLs; heavy PySide6 modules excluded. Console kept on so `--headless` works.
- **Smoke test passing:** `dist/gemini-live-agent/gemini-live-agent.exe --help` exits 0 from a clean directory; bundle is 153 MB.

## Phase 5 additions (GUI)

- **Usage panel** — status-bar label fed by `UsageUpdate` (total / in / out / cached tokens, est. cost).
- **Activity feed** — second tab logging connection changes, tool calls, approvals, results, resets, errors with timestamps.
- **Mic state machine** — `_MicState` OFF/LIVE/MUTED chip next to the status label; `Ctrl+M` mute shortcut; follows connection state, mode, and mute toggle.
- **System tray** — `QSystemTrayIcon` with Show/Hide, Mute, Quit; close button minimizes to tray when `minimize_to_tray` is enabled (new persisted `AppSettings` field); tray Quit forces a real exit.
- **Screen-share indicator** — red banner while a share-screen session is connected.
- **Accessibility** — `&Approve`/`&Deny`/`&Cancel` mnemonics on tool cards, accessible names on transcript/feed/meters/input.
- **Tests** — `tests/test_gui_phase5.py` (9 tests): usage panel, activity feed, mic chip transitions, share banner, tray hide/quit, card mnemonics.

## How to run right now

```powershell
cd C:\Users\Hp\projects\gemini-live-agent
.venv\Scripts\python -m gemini_live_agent --help

# GUI (default)
.venv\Scripts\python -m gemini_live_agent

# Terminal modes
.venv\Scripts\python -m gemini_live_agent --headless --mode text
.venv\Scripts\python -m gemini_live_agent --headless --mode voice

# With screen share
.venv\Scripts\python -m gemini_live_agent --mode both --share-screen
```

The first run migrates `GOOGLE_API_KEY` from `.env` into the Windows Credential Manager. If no key exists, it prompts for one. Installed shortcuts launch the GUI (`--no-headless`); a "terminal" shortcut is also installed.

## What's working

- **Packaging:** `pyproject.toml`, locked `requirements*.txt`, `uv` venv, ruff/mypy/pytest configs. PySide6 and pytest-qt are already declared as dependencies.
- **Logging:** JSON Lines rotating logs + stderr, `run_id` on every record.
- **Core vocabulary:** frozen dataclasses in `core/events.py` / `core/commands.py`.
- **Single-session headless driver:** connects, sends text, streams mic/screen, plays speaker, dispatches tools.
- **Tool layer:** explicit pydantic schemas, no docstring introspection, `web_search` deleted.
- **Policy layer:** Windows path confinement, deny globs, sensitive-name rules, gating mode, outbound redaction.
- **Audit + persistence:** append-only JSONL journal, SQLite store schema.
- **Media layer:** explicit device selection, bounded mic queue, speaker generation-counter flush fix, and barge-in stream abort on `server_content.interrupted`.
- **Entry points:** `python -m gemini_live_agent`, `gemini-live-agent` console script, and `python main.py` all work.
- **Phase 3 session lifetime:** `core/supervisor.py` reconnect loop, resumption-handle save/expire/seed, GoAway handling, classified backoff, failure budgets, in-flight tool-result delivery across reconnect.
- **Single-epoch driver:** `core/session.py` injects `ToolDispatcher`, `TurnState`, and media objects so the Supervisor can reuse them across epochs.
- **Resumption support:** `settings/settings.py` builds `LiveConnectConfig` with `SessionResumptionConfig`; `persist/store.py` persists handles and seeds prior turns on fresh reconnects.
- **Tool cancellation:** `core/dispatcher.py` can kill running shells via `tools/shell.cancel_shell`; cancelled ids never receive a `FunctionResponse`.
- **Barge-in:** `Speaker.flush()` aborts and restarts the PortAudio stream on interruption; `LiveSession` calls `flush()` before handling `server_content.interrupted`; `AppSettings` enables server-side VAD/activity detection so the server cuts off the model turn instead of queueing user speech behind it.
- **Fake Live harness:** `tests/fakes/fake_live.py` scripts socket drops, GoAway, rejected handles, rejected `fc_id`s, tool-call cancellation, and barge-in interruption.
- **Headless driver resilience:** `headless.py` drives `Supervisor` instead of a single `LiveSession`.

## Key design decisions locked in

- **Single process, asyncio core.** GUI will live on a `QThread` with Qt signals in, `loop.call_soon_threadsafe` out. `core/**` never imports PySide6; `app/**` never imports asyncio primitives.
- **Frozen dataclasses across the thread boundary.** No shared mutable state.
- **Meters polled, not pushed.** `AudioLevel` events exist but the GUI should poll `Microphone.level` / `Speaker.level` at ~30 Hz.
- **Transcription coalesced to 80 ms in core.** `TurnState` emits at most one `PartialTranscript` per source per window.
- **Typed text via `send_client_content`.** Verified working on `gemini-3.1-flash-live-preview` with `turn_complete=True`.
- **Supervisor owns the long-lived conversation.** `LiveSession` is a single-epoch object; the Supervisor creates one per reconnect and reuses the dispatcher, turn state, and media.
- **Resumption handles are persisted with a 110-minute TTL.** `Store` prunes expired handles and can seed prior turns as client content on fresh reconnects.
- **Tool results are delivered across reconnects.** If the original `fc_id` is rejected, the result is reinjected as a system-note client turn.
- **GoAway causes a hard reconnect, not overlapped.** Media stays alive; the old socket is closed and a new one is opened with the saved handle.

## Immediate next: manual Phase 6 gate + Inno Setup

Phase 6 code work is done (wheel fixed, run/turn/usage persistence wired,
PyInstaller onedir build passing its smoke test). What remains needs real
hardware and the real API:

### Manual Phase 6 gate — PASSED (2026-08-01)

- Installer ran silently; app landed in `C:\Program Files\GeminiLiveAgent` (157 MB).
- Installed exe launched from a clean temp dir (no source tree, no `.env`) —
  `--help` exit 0, and a **live API text turn completed**: connected, model
  replied "hello", clean disconnect.
- State verified under `%LOCALAPPDATA%\GeminiLiveAgent`: run row with
  `ended_at`, persisted turn text, and non-zero usage rows (prompt 2214 /
  completion 73 — confirms the `response_token_count` fix against the real API).
- Uninstall silent and complete (Program Files dir removed; `%LOCALAPPDATA%`
  state intentionally survives).
- **Not covered:** a real *voice* turn (needs a human at the mic) and the
  API-key prompt path (a key already exists in the credential store).

### Inno Setup installer

`packaging/installer.iss` is drafted (per-machine install under Program Files,
Start Menu + optional desktop shortcut, post-install launch) and **compile-tested**:
Inno Setup 6.7.3 produces `dist\installer\gemini-live-agent-setup-0.2.0.exe`
(50 MB) in ~40 s. To rebuild:

```
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

The installer itself has not been executed yet (installing to Program Files is
part of the manual Phase 6 gate below).

### Live wall tests still pending

- **Phase 3 gate:** 30-minute real session with `--share-screen`, crossing the
  ~10-minute connection wall repeatedly, recording reconnect count and gaps.
- **Phase 5 gate:** 20-minute streaming session while dragging the window and
  opening a native file dialog — zero audio dropouts; keyboard-only
  approve/deny; screen-reader pass.

### Risks / known issues

- **Old `scratch/` directory** at `C:\Users\Hp\.gemini\antigravity\scratch\gemini-live-agent` still exists. Remove it manually if still locked.
- **`.env` file still exists** in the repo root. It is ignored by git, but `settings.secrets.migrate_dotenv()` already moved the key into the credential store. Decide whether to delete the file or keep it as a dev-only convenience.
- **Directory/package name still has a hyphen.** No longer blocking: the flat layout builds fine under PyInstaller (Phase 6 smoke test passed). Only relevant if the project ever moves to a `src/` layout.
- **Live 30-minute wall test** from the Phase 3 verification gate has not been run yet. Run it before declaring Phase 3 fully validated in production.
- **`.claude/` worktree cleanup** was attempted: the `claude/adoring-wright-c18ce1` worktree metadata was removed from git, but the empty `.claude/worktrees/adoring-wright-c18ce1` directory could not be deleted because Windows reports it as "Device or resource busy" (likely held by a shell or explorer). Reboot or close the holding process, then delete `.claude/` manually.
- **Global hotkeys** (system-wide mute/push-to-talk) were not implemented in Phase 5 — no cross-platform hotkey dependency is declared. Revisit after packaging.

## Cut list (still valid)

Do not start these yet:

- Overlapped reconnect
- History browser GUI
- Diagnostics panel (log file only for now)
- Policy editor GUI (hand-edit JSON)
- Voice-announced approvals
- `tools/jobs.py` deferred-job table

## Running checks

```powershell
cd C:\Users\Hp\projects\gemini-live-agent
.venv\Scripts\ruff check .
.venv\Scripts\mypy .
.venv\Scripts\pytest -q
```

## Open questions

1. **Does `fc_id` survive real resumption?** The fallback (system-note reinjection) is implemented and tested; a live socket still needs to confirm the happy path.
2. **VB-Audio Virtual Cable loopback guard** currently warns. Change to hard-refuse if desired.
3. **Close button minimize-to-tray?** Resolved in Phase 5 — tray icon with Show/Hide, Mute, Quit; close minimizes to tray (persisted `minimize_to_tray` setting).
4. **Should the model be told the policy rules explicitly?** Currently only the workspace root and destructive-op approval are mentioned in the system instruction.
5. **Single `.exe` vs. Inno Setup?** Leaning onedir + Inno Setup per Context.txt; decide during Phase 6 packaging.
6. **Searchable transcript across runs?** Left out; Phase 6's incremental SQLite history is the prerequisite.

## Recommended first Phase 6 task

Run `python -m build` and inspect the produced wheel. Confirm every runtime
package (`app/`, `core/`, `media/`, `tools/`, `policy/`, `audit/`, `obs/`,
`persist/`, `settings/`, `gemini_live_agent/`) is included, and fix the
hatch `packages` target before writing the PyInstaller spec.
