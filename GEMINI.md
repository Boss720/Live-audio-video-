# GEMINI.md

Read and follow `AGENTS.md` in the repo root. It is the single source of truth for all agents here.

## Antigravity-specific

- Default to UI and browser-verification tasks. Save screenshots/recordings as artifacts and list them in `HANDOFF.md`.
- Working in parallel with Claude or Codex: use your own worktree (`agent/antigravity/<task>`), never the main checkout.
- Dispatched headless (`agy -p`, task file in `.codex-out\task-<task>.md`): stay inside your worktree, edit only the scope, do not commit or push; the runner commits and your final message becomes the commit message. Do not edit `HANDOFF.md` or `PLAN.md`: put findings in that final message.
- Interactive sessions only: write findings to `HANDOFF.md` under "From Antigravity". Never commit to `main`.
