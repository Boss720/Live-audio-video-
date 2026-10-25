# PLAN.md

## Review roadmap (2026-10-24) — ACTIVE

Source: review page `output/review-2026-10-24/live-agent-review.html`
(published https://claude.ai/artifact/HWD3tvVaRrmSZaPBQNrn2s). Baseline commit
`1490dd7` on `Leroy`: 406 passed, 3 skipped. Each module owns disjoint files so
agents can run in parallel. Commit per module after coordinator verification.

### Wave 1 — safety, privacy, hangs
- [x] M1 Safe defaults — `settings/settings.py`, `app/main_window.py` (wake-word fallback only).
      yolo=False, computer_use_enabled=False, model=gemini-3.8-live, map `minimal`
      thinking to `low` on extended-thinking, GUI wake-word fallback uses settings default.
- [x] M2 Privacy routing — `core/supervisor.py`, `core/tier_manager.py`.
      Startup/fallback/reset obey privacy_mode + prefer_free_tier; strict never uses Free;
      strict with only Free key fails with an actionable error.
- [x] M3 Session policy + wake gate — `core/session.py`.
      Build PolicyEngine only when no dispatcher injected; post-tool continuations are
      addressed; gated tool calls always get a FunctionResponse; no ungated dispatch
      before transcription.
- [x] M5 Turn order — `core/turn_state.py`.
      USER TurnComplete before MODEL; interrupted answers persisted and id advanced.

Wave 1 result: 439 passed, 3 skipped; runtime mypy 40 files + eval mypy 3 files clean; ruff clean.
Decided: prefer_free_tier default stays True (Free first), confirmed by user.
Live-socket checks still needed for M3 (see HANDOFF).

### Wave 2 — latency
- [x] Mic `latency="low"` + 40 ms default chunk (`media/microphone.py`, settings) — `cb2324b`
- [x] Speaker blocksize 480 + 40 ms prebuffer (start/flush/underrun), RMS in callback (`media/speaker.py`) — `cb2324b`
- [x] SQLite WAL/synchronous=NORMAL/busy_timeout + indexes + off-loop writer thread (`persist/store.py`; journal is JSONL, unchanged) — `5522e64`
- [x] GoAway zero backoff (with handle); failure log reset after 60 s stable; replay `turn_complete=False`; duplicate meter loop removed (`core/supervisor.py`) — `d9eafc8`
- [x] INTERRUPT scheduling for blocking_interrupt tools on extended thinking; frame wait 1.5 s on typed text;
      diagnostics 1 Hz, meters 15 Hz on change (`core/session.py`) — `634a8eb`
- [x] trigger_tokens default 100000; free-tier rows store 0 USD (`LiveSession(free_tier=)`); usage contract = per-turn increment
      (session sums, supervisor TPM records each total_tokens) — `cb2324b`, `634a8eb`, `d9eafc8`
- [x] `speech_end_to_first_audio_ms` timing (p50/p95 in runtime diagnostics; start = latest input transcription or mute stream end) — `634a8eb`

Wave 2 result: 471 passed, 3 skipped; runtime mypy 40 files + eval mypy 3 files clean; ruff clean.
Needs live-socket check: usage per-turn assumption (if API turns out cumulative, TPM overcounts), GoAway instant resume,
INTERRUPT on extended thinking, real-device prebuffer/blocksize, speech-end metric values.

### Wave 3 — accuracy and safety hardening
- [x] Frames: 1280 px (1568 computer use), q82, subsampling=0, cursor overlay, pixel-hash (+cursor) dedup, reused mss — `1bf366c`
- [x] Echo: gate mic (send silence) while speaker audible + 250 ms, unless headset; `echo_gate` auto|on|off. AEC later — `0f9ed8c`
- [x] Tools: description truth-up, le=300, read cap 64 KB + continue hint, `edit_file` search/replace (gated like write_file) — `9d550e4`
- [x] Redaction patterns; redact before truncate; shell head 2 KB + tail 6 KB; tree kill + reap on cancel/timeout; stdin DEVNULL — `af5372e`
- [x] Computer: clipboard restore, key-combo deny set; headless stdin EOF disconnect; wake word whole/joined tokens + Soundex — `c6ad674`
- [x] google-genai 2.28.0 -> 2.33.0 — `25e9cd7`

Wave 3 result: 561 passed, 3 skipped (each commit tested alone from the index); mypy both scopes clean; ruff clean.
Real capture smoke: 1280x720, ~169 KB/frame, 60 ms warm, unchanged screen skips encode; cursor visible.
Needs live check: echo gate on speakers vs headset detection names, frame accuracy for clicks at 1568 px,
headless EOF in voice mode, wake-word false positives in real ASR.

### Wave 4 — UI (mocks A, C, D)
- [x] Status strip, settings dialog, approvals rail, chat bubbles, diagnostics page
  - [x] W4.1 `app/status_strip.py` (model, tier, latency p50, cost, YOLO, screen)
  - [x] W4.2 `app/chat_view.py` (bubbles, inline tool rows, plain-text model output)
  - [x] W4.3 `app/approvals_rail.py` (risk LOW/MEDIUM/HIGH, deny all, cancel)
  - [x] W4.4 `app/settings_dialog.py` (all persisted fields, live vs reconnect, risky box)
  - [x] W4.5 `app/diagnostics_page.py` (speech end to first audio, frame dedup, export)
  - [x] W4.6 integrate into `app/main_window.py` (Ctrl+, opens settings), GUI tests green
  - Result: commits `cb3c5b4` `d6656ef` `cb37d7c` `68bd53b` `291e0c8` (components) + `8f816e6` (wiring).
    624 passed, 3 skipped; ruff and mypy clean. GUI launched; Settings, Diagnostics, risky box checked by eye.
- [x] W4.7 `DisconnectReason` emitted as an event (`7a63330`); Reconnects tile counts real drops.
- [x] W4.8 Mock B companion pill (`80220ad`) + mini mode wiring (`fbeb15b`, Ctrl+Shift+M). Unit tests only.

### Wave 5 — interrupt, frame budget, wake review, pill polish (spec: `docs/wave5.md`)
- [x] W5.1 `Interrupt` command, Esc / "Stop talking" (`409b086`). Fake-socket tests; not checked live.
- [ ] W5.2 frame byte budget in `media/screen.py` (Codex), then wire `max_bytes` in `core/session.py`.
- [x] W5.3 `scripts/wake_review.py` (`b2a6693`). First run on agent.log: 7 gated near misses, most
  "Roy" for "Leroy" (false negatives), 0 fuzzy addressed.
- [ ] W5.4 pill polish: border, "Stop talking" button, Esc (Antigravity), then wire
  `interrupt_requested` in `app/main_window.py`.

---


Baseline: branch `bongo`, commit `6153506`. 133 tests pass, ruff/mypy clean.
Source of external facts: `~/Downloads/gemini-live-integration-plan.md` (Aug 2026 Live API research).

Each module is independently shippable. Update the checkbox and add a one-line
result after EVERY module completes. Commit per module.

---

## Module 1 — Prompt/registry truth-up  (S, no deps)

- [x] Delete the `web_search(query)` entry from `_SYSTEM_INSTRUCTION_TEMPLATE`
      (`settings/settings.py:41`). The tool was removed from `tools/registry.py`;
      the prompt still advertises it, so the model can emit a call the dispatcher
      cannot serve.
- [x] Generate the "Your Tools" list from `tools/registry.py` declarations
      instead of hand-maintaining prose, so the two cannot drift again.
- [x] Expand `_AVAILABLE_VOICES` to the full 30 prebuilt voices (add at least
      Zephyr; current list is 5).
- [x] Test: assert every tool name in the rendered system instruction exists in
      the registry, and vice versa.

**Accept:** new test fails on today's `main`, passes after the fix.

**Result (done):** `settings/settings.py` renders `## Your Tools` via
`_generate_tools_list()` from `ALL_TOOLS` (+ `COMPUTER_TOOLS` when
`computer_use_enabled`); `web_search` gone; voices 5 → 30 (default `Puck` still
valid). New `tests/test_registry_sync.py` (5 tests) — 4 of them fail on the
pre-fix `settings.py`, all pass after. Suite 133 → 138 passed; ruff and mypy
(60 files) clean.

---

## Module 2 — Cost control for video  (M, no deps) — highest $ impact

Screen share is force-enabled whenever `computer_use_enabled` is on, and every
`computer_*` call pushes an extra frame (`core/session.py::_push_fresh_frame`).
On `gemini-3.1-flash-live-preview` the default `turnCoverage` rebills every
video frame into every turn; image input is $1.00/1M tokens.

- [ ] Add `turn_coverage` and `media_resolution` to `AppSettings` and set them in
      `as_connect_config()`. Default `turn_coverage` to the activity-only value
      so ambient frames stop being rebilled per turn.
- [ ] Frame dedup in `ScreenCapture`: hash the encoded JPEG, skip publishing when
      identical to the previous frame (idle desktop currently costs full price).
- [ ] Gate `_video_send_loop` on turn state: keep streaming while a turn is
      active or computer-use is running, throttle hard when idle.
- [ ] Test: dedup drops repeated frames; gating stops sends when idle;
      `as_connect_config()` carries the new fields.

**Accept:** measured token counts from a 5-minute idle screen-share session drop
materially vs. baseline (record both numbers here).

---

## Module 3 — Real cost accounting  (S, depends on nothing)

`UsageUpdate.estimated_usd` is declared (`core/events.py:146`) but **never
computed** — it is always `0.0`. The GUI status bar and the SQLite `usage` table
both persist a zero.

- [ ] Add a per-model price table (text/audio/image in, text/audio out) with a
      single source of truth and the pricing-date stamped in a comment.
- [ ] Compute `estimated_usd` in `_record_usage` / at `UsageUpdate` construction
      from the per-modality token counts.
- [ ] Test: known token counts produce the expected dollar figure; unknown model
      id degrades to 0.0 without crashing.

**Accept:** GUI shows a non-zero `~$` after one live turn; `Store.get_usage()`
rows carry it.

---

## Module 4 — Context-window compression tuning  (S)

`as_connect_config()` sets `sliding_window(target_tokens=16000)` with no
`trigger_tokens`, on a 128K-context model. Compression therefore fires on an
unknown default and truncates to 16K — conversational memory is thrown away far
earlier than needed.

- [ ] Make `trigger_tokens` and `target_tokens` `AppSettings` fields; default to
      100_000 / 60_000 per the research doc.
- [ ] Assert in tests that both are present in the built config.

**Accept:** a long session retains context past the current 16K cliff; document
observed turn count before/after.

---

## Module 5 — Model failover  (M, depends on M1 for prompt build)

`fallback_model` (`settings/settings.py:137`) is declared and **never
referenced**. The value it holds — `gemini-2.5-flash-native-audio` — is also not
a currently-published id; the live alternative is
`gemini-2.5-flash-native-audio-preview-12-2025`.

- [ ] Fix the default id, or set it to `None` until verified against the API.
- [ ] Wire the Supervisor: after N consecutive `not_found` / hard-`INTERNAL`
      failures on the primary model, reconnect on `fallback_model` and emit a
      user-visible event.
- [ ] Test with `tests/fakes/fake_live.py`: primary fails the failure budget,
      supervisor reconnects on the fallback id, and stays there.

**Accept:** fake-harness test proves the switch; live check that the fallback id
actually connects.

---

## Module 6 — 1011 telemetry and handling  (M, depends on M5's classifier work)

HANDOFF records WS close **1011** as the known 3.1-preview failure mode (and the
reason native `computer_use` was abandoned). `_classify_disconnect`
(`core/session.py:775`) has no 1011-specific branch — it falls through to the
generic `transient` bucket, so the failure budget cannot distinguish "model is
unstable under audio+video" from "network blipped".

- [ ] Add an explicit `internal_1011` classification (read the websocket close
      code where available, not just the exception text).
- [ ] Give it its own failure budget and backoff curve in `Supervisor`.
- [ ] Log/emit a counter so the rate is visible in the activity feed and logs.
- [ ] Test: fake harness closes with 1011; assert the dedicated category, its
      backoff, and that the resumption handle is retried before being dropped.

**Accept:** a scripted 1011 storm reconnects on the intended curve instead of the
generic transient one.

---

## Module 7 — Live wall tests (manual, real hardware + real API)  (L)

Still outstanding from Phase 3/5 and the only true unknowns. Nothing below is
substitutable by unit tests.

- [ ] **30-minute session with `--share-screen`**, crossing the ~10-minute
      connection wall repeatedly. Record: reconnect count, gap duration per
      reconnect, whether `fc_id` survived resumption (open question 1 in
      HANDOFF), total tokens and $ (validates M2 + M3).
- [ ] **Real voice turn** end-to-end on the installed build — never yet run; the
      Phase 6 gate covered text only.
- [ ] **API-key prompt path** on a machine with no credential-store entry.
- [ ] **20-minute GUI streaming session** while dragging the window and opening a
      native file dialog: zero audio dropouts, keyboard-only approve/deny,
      screen-reader pass.
- [ ] Record all results in HANDOFF.md.

**Accept:** every item has a written observed result, pass or fail.

---

## Module 8 — Documentation reconciliation  (S)

- [ ] HANDOFF.md header says branch `Kimi-V2` / commit `c7d622d`; actual is
      `bongo` / `6153506`. Fix, and note the wake-word, GUI-prompt, and
      computer-use commits that landed after.
- [ ] Delete or explicitly justify the repo-root `.env` — the key already lives
      in the Windows Credential Manager (HANDOFF "Risks" item).
- [ ] Record that `~/Downloads/gemini-live-integration-plan.md` targets a
      *different* repo (`bosscube1/Max-PRO`) so a future reader does not chase
      its web-app phases as work items for this project.

---

## Explicitly NOT doing (from the research doc)

The Downloads plan is written against `bosscube1/Max-PRO`, a small single-file
Python CLI, and most of its bulk is a browser port. This repo is a packaged
PySide6 desktop app plus a headless CLI, already past that plan on every
hardening axis it names (session resumption, VAD config, backoff, compression,
barge-in, transcription — all shipped).

- **Ephemeral tokens / `liveConnectConstraints`** — mitigates shipping an API key
  to a browser. There is no browser. The key is in the Windows Credential
  Manager. Only revisit if a web/remote client is actually wanted.
- **React + Vite web app, AudioWorklet, `getDisplayMedia`, token backend** — a
  second product, not an improvement to this one. Would also mean re-deciding
  whether `run_command` / computer-use may be driven remotely, which is a
  security question, not a porting question.
- **Playwright E2E** — no browser surface to drive.

Adopt from the doc only: the cost levers (M2), the compression numbers (M4), the
1011 handling (M6), and the pricing table (M3).

---

## Suggested order

M1 → M3 → M2 → M4 (all independent, cheap, immediately measurable)
then M5 → M6 (share the classifier/supervisor surface)
then M7 (needs the M2/M3 instrumentation to produce useful numbers)
M8 anytime.
