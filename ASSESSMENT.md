# Codebase assessment and improvement plan

Assessed 2026-10-05 on branch `Leroy`, HEAD `5f7eb91`. This is a proposal;
runtime code has not been changed. Existing local `.gitignore`,
`bridge_stdio.py`, and `tests/test_bridge_stdio.py` changes were preserved.

## Overall assessment

The project has a substantial working foundation: separated desktop/core/media/
policy/storage modules, typed command and event contracts, a conversation
supervisor, reconnect and tool-result recovery, keyring-backed credentials,
approval cards, screen feedback, and a useful fake Live API test harness.
A rewrite would discard that investment. Improve behavior and verification
incrementally before adding more capabilities.

The principal weakness is that tests cover individual mechanisms better than
their interactions: wake-word checks with tool calls, GUI configuration with
runtime settings, privacy policy with fallback, and cancellation with resource
ownership. Passing tests currently leave important end-to-end invariants open.

`PLAN.md` and `HANDOFF.md` are historical records rather than a reliable current
backlog. Reconcile them against this checkout before continuing the old modules.

## Fresh verification

Coordinator independently executed:

| Check | Result |
| --- | --- |
| `.venv\Scripts\python.exe -m mypy .` | Exit 0; strict configuration; no issues in 74 source files |
| `.venv\Scripts\python.exe -m ruff check .` | Exit 0; all checks passed |
| `.venv\Scripts\python.exe -m pytest -q --tb=short` | Exit 0; 243 passed in 16.08 seconds |

The first sandboxed pytest run produced 243 setup errors because the default
temporary directory was inaccessible. A workspace `--basetemp` retry was also
blocked after the path resolved to the canonical D: checkout. The full passing
run used approved execution outside that filesystem sandbox. Neither failed
attempt establishes a product failure.

No live provider call, microphone session, real desktop action, new package
build, installed executable smoke test, or visible-window focus check was run.
The pytest result includes Qt tests but does not prove these live behaviors.

Coordinator-owned in-memory/recording-fake probes additionally produced:

```text
Unaddressed wake-enabled tool dispatch: [('probe', 'read_file', {'path': 'harmless'})]
Wake gate evaluated: False
Strict privacy fallback tier: free
Free-tier forwarded estimated USD: 0.123
EOF reader stopped: True queued signals: 0
EOF connect waiter: TIMEOUT
Accumulated user transcript: first user turnsecond user turn
GUI loaded monitor/frame/privacy: 2 on_demand strict
GUI rebuilt monitor/frame/privacy: 1 stream auto
Shell exit 7 marked successful: True
Recent 2 of 4 tool IDs: ['0', '1']
```

The wake probe recorded dispatcher submissions; it executed no actual tool.
Tier probes used synthetic keys and invoked no network connection. The user
transcript probe showed two user partials with ID 0 and no USER completion.

## Findings, prioritized

P1 means fix before relying on the affected unattended/privacy/cancellation
behavior. P2 means the next correctness or usability milestone. Source paths
below are relative to this report's repository.

| Priority | Finding and consequence | Evidence |
| --- | --- | --- |
| P1 | Wake gating can dispatch a tool-only response to unaddressed speech. The gate evaluates only on model text/audio, leaving `_turn_gated` false for a direct tool request. A turn boundary also resets the gate before later tool handling in the same message; add that ordering case to regression tests. | `core/session.py:543`, `:557`, `:719`, `:735`; coordinator recording-fake reproduction |
| P1 | Strict privacy/free preference settings do not govern supervisor routing. Initial selection directly prefers Tier 1; fallback considers Free without applying privacy constraints. `TierManager.select_tier()` contains policy that the supervisor does not use. | `settings/settings.py:293`, `core/supervisor.py:148`, `:610`, `:674`, `core/tier_manager.py:112`; coordinator strict-fallback reproduction |
| P1 | GUI Connect/Save rebuilds settings from visible controls and loses hidden loaded values. Monitor 2, on-demand frames, token budgets, privacy, search/thinking settings, and explicit tier keys can revert. This also undermines fixes that depend on persisted configuration. | `app/main_window.py:442`, `:577`; coordinator unbound-method reproduction with recording controls |
| P1 | Cancellation cleanup is incomplete. Session loop cancellation occurs after normal `asyncio.wait()` return rather than in unconditional cleanup. Shell task cancellation removes process tracking without killing/reaping the process; direct shell cancellation kills only the immediate process. | `core/session.py:503`, `:540`; `tools/shell.py:78`, `:87`, `:118`; worker recording-task/process probes, independently inspected source |
| P1 | Tool arguments are written raw to journal and SQLite. Executed results are redacted, but validation/denial results reach `_finish()` without the same redaction. Pydantic validation errors can echo input. | `core/dispatcher.py:183`, `:202`, `:248`, `:329`, `:352`; `audit/journal.py:103`, `persist/store.py:214`; worker synthetic-secret memory probe, independently inspected source |
| P1 | Existing final symlinks are not resolved for writes. Validation resolves only the parent, while the write opens the final path normally. That can invalidate allow-root, denied-name, or immutable-target checks. | `policy/engine.py:187`, `tools/fs.py:79`, `:85`; source finding, no Windows filesystem exploit demonstrated |
| P1 for embedding | The local stdio bridge does not enqueue EOF. Before connect it waits forever; during a session it never requests disconnect when the host closes stdin. Boolean strings are coerced unsafely (`"false"` becomes true), and malformed collection-valued modes can raise uncaught `TypeError`. | `bridge_stdio.py:103`, `:177`, `:205`, `:252`, `:291`; coordinator EOF/parser probes. This bridge and its test are existing untracked work |
| P2 | Voice user turns never complete: their ID stays at zero and accumulated text grows across utterances. Persistence accepts `TurnComplete` events, so spoken user history is missing. Persisted turn IDs also need source qualification when USER completion is added. | `core/turn_state.py:61`, `:98`; `core/session.py:692`; coordinator two-exchange reproduction |
| P2 | Free-tier events retain paid estimates. Cost calculation uses a single input/output rate pair despite collecting modality details. Each usage modality row receives the entire cumulative cost, so summing rows would overcount. Usage handling also assumes incompatible increment/cumulative semantics in session and supervisor; establish the provider contract before changing arithmetic. | `core/session.py:786`, `:1258`; `core/supervisor.py:546`, `:550`; `app/main_window.py:530`; coordinator free-tier event reproduction and direct source inspection |
| P2 | Concurrent frame triggers can pass the same interval/digest checks before either updates shared state, sending duplicate frames. | `core/session.py:1085`; worker concurrent recording-fake probe; source inspected independently |
| P2 | Shell exit failures still return `ToolResult(ok=True)`, and bounded “recent” tool history selects the oldest rows. | `tools/shell.py:103`, `persist/store.py:350`; coordinator fake-process/in-memory probes |
| P2 | Free-only credentials are rejected by GUI startup and headless key prompting, even though supervisor routing supports Free. GUI and CLI expose five voices while settings validate thirty. | `app/main_window.py:818`, `main.py:190`, `main.py:28`, `app/main_window.py:81`, `settings/settings.py:19`; source inspection |

The shell is a separate trust boundary: file-tool policy checks its working
directory, not everything command text can access. Approval and YOLO are user
permission controls; they are not operating-system isolation. Decide whether
trusted full-user shell execution is the intended product contract, and state
that accurately. If constrained execution is required, use a real isolation
mechanism rather than relying on command-string matching. Also stop
`tools/_state.py:27` from retaining old workspace roots on workspace changes.

## Reconciliation with the existing roadmap

| Historical item | Current state | Remaining work |
| --- | --- | --- |
| Prompt/registry synchronization | Implemented and tested | Share the voice catalog with CLI/GUI; keep capability tests |
| Video cost controls | Turn coverage, media resolution, on-demand mode and dedup exist | Serialize concurrent sends; measure real idle/active usage before selecting defaults |
| Cost accounting | Estimates now exist | Tier/modality/usage-contract correctness and clear Free/unknown display |
| Compression tuning | Trigger and target fields exist | Apply/preserve tier-specific settings and verify long-session behavior |
| Failover | Rate-limit tier/model cascade exists | Enforce routing policy; separate unsupported model/config errors from transient failures |
| Dual-tier GUI integration | Still incomplete | Tier status, privacy controls, TPM headroom, explicit cooldown state and key management |
| Manual live gates | Historical evidence is recorded, but several hardware/long-session gates remain open | Run fresh gates on the newly built release |
| Packaging | Wheel/spec/installer definitions exist | Reproducible clean build and fresh installed-artifact proof |

Do not copy historical model availability, prices, or quota limits into new
requirements as current facts. Verify them against official provider sources
and a controlled live probe in the relevant implementation milestone.

## Proposed implementation sequence

### Model migration addendum — Gemini 3.8

Official documentation checked 2026-10-05 identifies two stable options:

- [`gemini-3.8-live`](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live):
  target for fast conversation. Omit thinking configuration and affective-dialog
  configuration; update the existing search capability gate.
- [`gemini-3.8-live-extended-thinking`](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live-extended-thinking):
  target for multi-step agent work. Requires NON_BLOCKING tools, no function
  response scheduling, and low/medium/high thinking. Task completion uses
  `interaction_status`; utterance completion does not establish idle.

Local inspection found google-genai 2.16.0 with no declared interaction-status
field, unconditional thinking configuration, blocking computer tools, and
wake-gate reset at utterance completion. These require an explicit compatibility
layer and SDK/schema validation. Preserve authorization across intermediate
speech and serialize dependent desktop actions even when protocol tools are
non-blocking.

Add standard 3.8 configuration support alongside milestone 1; integrate
Extended Thinking after milestone 2's lifecycle/gating fixes. Start its task
profile at medium as an evaluation hypothesis. Refresh model-specific pricing,
quotas and fallback policy. Validate both with setup, voice, tool, interruption
and reconnect tests plus controlled live probes. Neither model has been called
with this checkout or the user's credentials during this assessment.

Effort is relative: S = bounded change, M = multiple interacting modules,
L = integration plus release/live validation. Each milestone should have a
failing regression for its actual defect, a scoped commit if requested, and
coordinator-owned combined-diff/full-suite/static-check verification.

### 1. Preserve configuration and enforce routing (M)

- Derive GUI settings from the loaded model, updating only edited fields;
  revalidate the merged model instead of bypassing validators.
- Apply one routing policy to startup, fallback, and full-backoff reset.
  Respect strict privacy, free preference, modality and tier token budgets.
- Resolve either supported key at startup; keep secrets out of settings JSON.
- Add explicit tier/privacy/usage/cooldown events suitable for the UI.

Acceptance: non-default hidden settings survive Save/Connect/reload; strict
runs never use Free; strict+only-Free fails with an actionable error; free-only
relaxed startup succeeds; configured preference has the documented effect.
Key/value choices are tested with fakes before controlled live verification.

### 2. Close tool and lifecycle gaps (L)

- Decide addressing before tool effects, including tool-only, delayed-ASR and
  combined boundary/tool messages; preserve the typed-input exemption.
- Make session-task and subprocess cleanup unconditional. On Windows, own
  subprocess trees and handle cancellation during spawn and communication.
- Centralize sanitized external/persisted representations of tool calls;
  retain original executable arguments only where needed in memory.
- Validate actual write targets, including final symlinks/reparse points;
  revoke previous workspace roots when switching workspaces.
- Return shell success based on the exit status. Document the shell trust
  boundary and decide whether OS isolation is within scope.

Acceptance: unaddressed speech dispatches zero tools; cancel/disconnect leaves
zero owned tasks/processes; synthetic credentials are absent from model
responses, UI events, logs and database; outside/denied/immutable link targets
are rejected while ordinary new files remain writable; nonzero exit is failure.

### 3. Repair conversation and accounting semantics (M)

- Define USER turn boundaries for ordinary speech, interruption and typed
  input; persist distinct source-qualified IDs.
- Define newest-N recovery semantics with stable ordering for transcripts and
  tool results.
- Establish the Live usage contract, convert it once into consistent deltas
  and totals, and retain tier/model/modality with the records.
- Distinguish Free, unknown cost and estimated paid cost; do not store repeated
  whole-session estimates as additive per-modality expenses.
- Serialize frame capture/gating/send while preserving forced post-action
  feedback and frame freshness.

Acceptance: two exchanges persist separate user/model records; recovery beyond
the limit returns the latest relevant history; synthetic cumulative and
incremental fixtures yield the intended totals; Free has no paid estimate;
concurrent normal triggers send one identical frame and forced feedback works.
Then compare real idle/active screen usage and observed accounting.

### 4. Finish embedding and desktop controls (M)

- If the local stdio bridge is intended for delivery, introduce explicit EOF,
  bounded queues/frames, strict command schemas, and monitored pump failures.
  Add process-level lifecycle tests and include its entrypoint in packaging;
  it is absent from the current wheel include list.
- Share the voice catalog, expose monitor selection, and surface active
  tier/model, privacy, Free/unknown/paid estimates, TPM and cooldown state.
- Keep shortcuts consistent and make approval/deny/cancel keyboard reachable.

Acceptance: host EOF before and after connect exits cleanly; invalid protocol
frames yield recoverable errors without unsafe coercion; stdout remains valid
NDJSON; distribution-installed entrypoint works; controls are mounted, visible
and tested with no shortcut collisions. Validate real desktop reachability.

### 5. Automate release proof and refresh documentation (M/L)

- Add Windows CI for locked dependencies, strict mypy, Ruff and the full suite.
- Pin the build toolchain; derive application, executable and installer version
  metadata from one source; inspect wheel contents and build the installer.
- Smoke-test from a clean working directory with isolated test state. Verify
  the visible installed window, first-run key flow and writable workspace.
- Run a human microphone turn, barge-in/approval flow, a 30-minute reconnect
  session and the existing 20-minute GUI responsiveness gate. Record gaps,
  reconnects, task/process counts and usage rather than just “passed”.
- Refresh README/PLAN/HANDOFF to match the release, with implemented features
  separated from still-open live gates.

Acceptance: fresh CI and build logs, installed-artifact smoke evidence, a live
validation record with explicit limits, and docs matching actual controls and
commands. No historical build or fake harness result substitutes for this gate.

## Suggested army ownership for implementation

Keep one coordinator owning `PROGRESS.md`, plan reconciliation and integration.
Use isolated worktrees or exclusive file ownership, with at most three workers
plus the coordinator in this runtime.

- Runtime lane: `core/session.py`, `core/supervisor.py`, `core/turn_state.py`,
  `core/tier_manager.py`, media modules and corresponding tests.
- Tool-policy lane: `core/dispatcher.py`, `tools/`, `policy/`, `audit/`,
  `persist/` and corresponding tests.
- Desktop/delivery lane: `app/`, entrypoints, packaging/manifests/docs and
  corresponding tests.

`core/events.py`, `core/commands.py` and `settings/settings.py` are shared
contracts: freeze their changes with one owner before parallel consumers begin.
Do not simultaneously edit these files across lanes. The local bridge work
must be reconciled with its existing author before implementation overwrites it.

Start with milestones 1–2. Defer large structural refactors, overlapped
reconnect, new model features and a history browser until these invariants and
release gates are reliable. No implementation is authorized by this assessment.
