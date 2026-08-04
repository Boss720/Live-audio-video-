# PLAN.md — Post-Phase-6 Improvement Plan

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
