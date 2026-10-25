# Wave 5: interrupt, frame budget, wake review, pill polish

Source of truth for Wave 5 workers. Branch `Leroy`. Python 3.11 venv at
`D:\AI\projects\Dev\Projects\gemini-live-agents\gemini-live-agent\.venv`.
Test command and shared files: see `AGENTS.md` "Project commands".
Workers never edit shared files; the orchestrator wires them after merge.

## W5.1 Interrupt command (orchestrator)

- `core/commands.py`: new `Interrupt` command (stop the model's current reply).
- `core/session.py`: on `Interrupt`, flush the speaker and, if the reply is
  still streaming (first audio seen this turn), drop its remaining audio. The
  flag clears at the turn boundary or a server `interrupted`. Transcripts are
  kept (the chat shows the whole reply). The session stays connected.
- `app/main_window.py`: Interrupt action (Esc while connected) and pill wiring
  (`CompanionPill.interrupt_requested`).

## W5.2 Frame byte budget (Codex) — `media/screen.py`, `tests/test_screen_budget.py`

Live data: frames at 1568 px average ~294 KB at quality 82. Add an optional
byte budget so large frames step their JPEG quality down.

- New constants: `DEFAULT_MAX_BYTES = 200_000`, `MIN_QUALITY = 50`,
  `QUALITY_STEP = 10`.
- `_encode(..., max_bytes: int | None = None)`: encode at `quality`; while the
  result is larger than `max_bytes` and quality > `MIN_QUALITY`, re-encode at
  `max(MIN_QUALITY, quality - QUALITY_STEP)`. Resize happens once, before the
  loop. Return the last encode even if still over budget. `max_bytes=None`
  keeps today's behaviour exactly (one encode).
- `JpegFrame` gains an attribute `quality: int` (the quality actually used;
  unchanged frames report the requested quality).
- Thread `max_bytes` through `_OnDemandGrabber._grab`/`grab`, `capture_jpeg`
  (keyword, default `None`) and `ScreenCapture.__init__` (keyword, default
  `None`, stored as `self.max_bytes`, passed to `_encode` in `_capture`).
- Existing signatures keep working positionally. No new dependencies.

## W5.3 Wake review script (Codex) — `scripts/wake_review.py`, `tests/test_wake_review.py`

`agent.log` is JSONL. Wake lines look like:
`{"timestamp": "...", "level": "INFO", "logger": "gemini_live_agent.core.session",
"message": "[WAKE] GATED (no response sent) wake='max' aliases=[] heard='Hello.'",
"run_id": "..."}` and the same with `[WAKE] ADDRESSED`.

- CLI: `python scripts/wake_review.py [LOG] [--run RUN_ID|--last] [--json]`.
  Default LOG: `%LOCALAPPDATA%\GeminiLiveAgent\Logs\agent.log`. `--last` = the
  last run_id that has wake lines.
- Parse `wake`, `aliases` (Python list literal, `ast.literal_eval`), `heard`
  (may contain escaped quotes; parse with a regex anchored on `heard='` to the
  final `'`).
- Classify each line:
  - `addressed_exact`: ADDRESSED and the normalized wake word or an alias is a
    whole word in the normalized heard text.
  - `addressed_fuzzy`: ADDRESSED otherwise (possible false positive).
  - `gated_near_miss`: GATED and some word in heard is within Levenshtein 3 of
    the wake word or shares its Soundex code (possible false negative).
  - `gated`: everything else.
  Use `core.wake_word._normalize`, `_levenshtein`, `_soundex` (import with the
  repo root on `sys.path`).
- Output: counts per class, then each `addressed_fuzzy` and `gated_near_miss`
  line (timestamp, wake, heard). `--json` prints one JSON object
  `{"counts": {...}, "addressed_fuzzy": [...], "gated_near_miss": [...]}`.
- Tests build a small log in `tmp_path`; no real log needed.

## W5.4 Pill polish (Antigravity) — `app/companion_pill.py`, `tests/test_gui_companion_pill.py`

- The rounded background is hard to see over dark windows: add a 1 px border in
  a theme token that contrasts with both light and dark (use `app/theme.py`
  tokens if present; do not edit `app/theme.py`).
- New signal `interrupt_requested = Signal()` and a small "Stop talking" button
  next to Hold, enabled only while connected (`set_connected`). Esc on the
  focused pill emits `interrupt_requested`.
- The existing Stop button keeps emitting `stop_requested`; change its text or
  tooltip to "End session" so the two are not confused.
- Keep all existing signals and methods, and keep labels plain text.
