@AGENTS.md

## Claude-specific

- Session start injects `trinity status -Brief`. If a board task says `done`, offer to `trinity merge` it.

## Auto-delegation (do this without being asked)

Codex and Antigravity are your subagents (`.claude/agents/`). Route by strength, run them in the background, keep working on your own part meanwhile.

| Work | Send to | Why |
|---|---|---|
| Self-contained coding task with a clear file scope (new module/format, bug with repro, tests) | `codex-worker` | Codex is strong at focused implementation; runs in its own worktree |
| Review before any commit, before any merge, second opinion after 2 failed attempts | `codex-reviewer` | Independent model catches what you miss |
| UI/frontend code (HTML, CSS, renderer), tasks needing a big read of the repo | `antigravity-worker` | Gemini headless (agy) in its own worktree; second strong model, large context |
| Visual check of the running app (click-through, screenshots) | you | Launch it and use computer use; headless agents cannot see a GUI |
| Cross-cutting design, shared files, merge, final verification, anything touching `main` | you | You hold the whole picture |

No ChatGPT plan: if the session-start `agents:` line says `codex unavailable`, send Codex-type work to `antigravity-worker` (dispatch reroutes anyway) and keep using `codex-reviewer` / `scripts/codex-review.cmd`: they fall back to Claude Code headless or Antigravity and print `reviewer: <agent>`.

Rules:
- Split by files first. Two agents never get overlapping scopes; shared files (the "Shared files" line in AGENTS.md: registries, package.json) stay with you, wired in after merge.
- Give subagents complete prompts: they start cold. Include paths, conventions, and the test command.
- Several independent tasks: launch all subagents in one message so they run concurrently.
- When a `codex-worker`/`antigravity-worker` returns: read its diffstat, then `./trinity.sh merge <task> -Yes` (runs Codex review + tests; from Bash always use `./trinity.sh`, see AGENTS.md), then wire shared files (see `.codex-out\deferred-<task>.txt` if merge printed one) and re-run the Test command yourself.
- Do the work yourself when it is small (under ~15 minutes), tangled across many files, or needs this conversation's context.
- Never paste secrets or the user's personal data into delegated prompts.

- You are the orchestrator. Plan in `PLAN.md`, hand off via `HANDOFF.md`, merge `agent/*` branches.
- Before every commit on `main`: run `./scripts/codex-review.cmd` (or `--base main` for a branch), fix real findings, ignore style nits. Takes minutes: run it in the background.
- Stuck on a hard bug after two attempts: `echo "..." | ./scripts/codex-ask.cmd`, then verify its answer yourself. Never pass the prompt as a quoted arg from Bash.
- UI change: after it builds, launch the app and click through it yourself (computer use), screenshot as evidence. `trinity ui` opens the IDE if the user wants to look.
- Treat Codex/Gemini output as a draft. Re-run tests and read the diff yourself.
