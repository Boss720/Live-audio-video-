---
name: army
description: Deploy a fleet of parallel subagents to chew through work fast and cheap. Use this whenever the user says "army", "deploy the army", "fan out", "parallelize this", "use subagents", "swarm it", or asks for anything that decomposes into 2+ independent chunks — auditing many files, researching several topics, applying the same edit across a codebase, generating many variants, or summarizing a pile of documents. Also use when the user wants a big job done cheaply, since this skill routes every chunk to the cheapest model that can actually do it, including Codex and Antigravity workers via the trinity toolkit, and to monitor them on the trinity dashboard.
---

# Army

Take a job, split it into independent chunks, dispatch each chunk to a subagent, and merge the results. Two goals at once: wall-clock speed (chunks run concurrently) and cost (each chunk goes to the smallest model that can do it).

The user invoked this skill, so spawning subagents is explicitly authorized — no need to ask permission to fan out.

## The loop

1. **Scout inline first.** Before spawning anything, find the actual work-list yourself: glob the files, list the topics, read the diff. Subagents start cold and re-derive context you already have, so every fact you hand them is money saved. A fan-out over a guessed work-list is worse than no fan-out.
2. **Split into independent chunks.** Chunks must not depend on each other's output. If chunk B needs chunk A's result, that's two waves, not one fan-out.
3. **Assign a worker per chunk**: a Claude model from the ladder below, or Codex / Antigravity (see "External workers") when the repo has trinity.
4. **Dispatch** — all Agent calls and `trinity dispatch` calls in a single response block so they run concurrently.
5. **Merge** — collect, dedupe, resolve contradictions, report one coherent answer.

## Model ladder — pick the cheapest rung that clears the bar

Start at the bottom and only climb when the task genuinely needs it. Overpaying on a 12-agent fan-out multiplies the waste twelvefold, which is the whole reason this skill exists.

| Rung | Model | Give it |
|---|---|---|
| 1 | `haiku` | Mechanical, verifiable work: grep-and-report, extract fields, rename symbols, reformat, "does this file contain X", summarize one short doc, tabulate. Anything where a wrong answer is obvious on sight. |
| 2 | `sonnet` | Normal coding and analysis: write or edit a module, review a file for bugs, research a topic and synthesize, trace a call path. This is the default when unsure. |
| 3 | `opus` | Genuinely hard reasoning: architecture decisions, gnarly multi-hypothesis debugging, security judgment, anything where a subtly wrong answer would survive review. Rare — usually at most one agent in a fan-out needs this, often none. |

Rules that keep cost honest:

- **Read-only scouting is almost always `haiku`.** Locating code is pattern matching, not reasoning.
- **A big chunk does not mean a big model.** Volume is the subagent's problem; difficulty picks the rung. Twenty files of mechanical renaming is still `haiku`.
- **Split by difficulty, not just by size.** If one file in a review is the hairy one, give that file its own `opus`/`sonnet` agent and let `haiku` sweep the other nineteen. Uniform model assignment across a non-uniform job is the most common way to overpay.
- **Prefer the narrow agent type over the general one.** Project-specific agents (see the available-agent-types list) come pre-loaded with instructions, so they need fewer tokens to reach the same answer than `general-purpose` does.
- **Also drop `effort` when you drop the model.** Low reasoning effort on mechanical chunks compounds the savings.

Say the assignment out loud in one line before dispatching — e.g. "12 chunks: 10× haiku sweep, 1× sonnet on the auth module, 1× opus on the token-refresh race." The user should be able to veto a rung before spending.

## External workers: Codex and Antigravity

The army has two more soldiers besides Claude subagents: OpenAI Codex and Google Antigravity (Gemini, via the `agy` CLI). They run headless in their own git worktree, on another vendor's quota, so they cost no Claude tokens beyond a short dispatch. Use them whenever the repo has the trinity toolkit (`trinity.cmd` at the root, agent defs in `.claude/agents/`).

| Chunk | Send to | How |
|---|---|---|
| Self-contained coding task with a clear file scope: new module, bug with repro, tests, scripts | Codex | `codex-worker` agent, or `trinity dispatch codex <task> -Scope "<glob>" -PromptFile <file>` |
| UI/frontend code, single-file pages, CSS, big-context reading across the repo | Antigravity | `antigravity-worker` agent, or `trinity dispatch antigravity <task> -Scope "<glob>" -PromptFile <file>` |
| Review before commit or merge, second opinion after two failed attempts | Codex (read-only) | `codex-reviewer` agent, `scripts\codex-review.cmd`, `trinity ask` |
| Read-only scouting, mechanical sweeps, synthesis that needs this conversation | Claude subagent | the ladder above |
| Shared files, cross-cutting design, merge, final verification, anything on `main` | you | never delegated |

No ChatGPT plan (Codex missing or logged out)? `trinity status` prints an `agents:` line. If it says `codex unavailable`, the army is Claude + Antigravity:
- Codex-type chunks go to Antigravity (`trinity dispatch codex` reroutes automatically with a notice; prefer `antigravity-worker` directly).
- Reviews (`codex-reviewer`, `scripts\codex-review.cmd`, `trinity review/ask/merge`) fall back to Claude Code headless (if `claude auth status` is logged in), else Antigravity in read-only plan mode, else the merge is gated by tests alone (yellow warning).
- `TRINITY_DISABLE=codex` forces this mode; `TRINITY_REVIEWER=claude|antigravity|none` picks the reviewer.
- Symmetric: without Antigravity, UI chunks reroute to Codex.

Rules for external workers:

- **Trinity first.** If the repo lacks `trinity.cmd`, run `trinity init <repo>` from your Holy trinity toolkit checkout before dispatching, and fill the Test command in its AGENTS.md so merges are gated.
- **Write a spec to disk first** (e.g. `docs/<feature>.md` with the contract) and commit it, so every worker reads the same source of truth. Prompts go in `.codex-out/prompt-<task>.md` and are passed with `-PromptFile` (quoted args break on paths with spaces).
- **One worker per file set.** Pass `-Scope` with exact paths. Shared files (registries, index files, package.json, the repo's AGENTS.md "Shared files") stay with you and get wired in after merge.
- **Dispatch, then keep working.** `dispatch` returns immediately. Run `trinity wait <task> -Timeout 2400` in the background (exit 0 done, 1 failed, 2 still running) and do your own chunk meanwhile.
- **Track your own chunk** with `trinity track <task> <note>` and finish it with `trinity done <task> <summary>`, so the board shows all three agents.
- **From Git Bash** (the Bash tool) call every trinity command as `./trinity.sh ...`: a note, summary or question with spaces breaks `trinity.cmd` there when the repo path has a space. A repo without `trinity.sh` gets it from `trinity init`.
- **Watch** with `trinity dashboard` (live page at `http://localhost:7777/`, all trinity repos, one lane per agent: models, tokens, tool calls, each worker's chat, Claude sessions and subagents) or `trinity status`.
- **Merge through the gate:** read the worker's result (`.codex-out\result-<task>.md`) and diffstat, then `trinity merge <task> -Yes` (review by Codex, or the fallback reviewer, blocks on P0/P1, then runs the Test command).
- **A failed run with work in the worktree** (status `failed (exit 2)`): the runner's commit was refused, usually by the pre-commit secret scan on test fixtures. Fix it in the worktree and commit there; never `--no-verify`. Never `git merge` an `agent/*` branch by hand. `trinity cancel <task>` kills a runaway worker.
- **Main must be clean to merge.** Commit your own chunk first (after its Codex review). If the gate fails on a worker branch, fix it in that worker's worktree (`..\<repo>.worktrees\<agent>-<task>`; `git merge main` there first if main moved), commit there, and re-run `trinity merge`. Don't touch main's working tree while a merge runs.
- **Treat their output as a draft.** Re-run the tests and read the diff yourself after merging. Workers cannot finish their own task: only the runner marks it done.
- Never paste secrets or the user's personal data into a worker prompt.

When saying the assignment out loud, include them: "4 chunks: Codex on the server + tests, Antigravity on the UI page, 1x haiku sweep, I take the shared wiring."

## Sizing the army

Match the fleet to the job, not to your enthusiasm. Each agent has real cost and the results all land in your context.

- 2–4 agents: most tasks.
- 5–12 agents: broad sweeps — per-file audits, per-topic research.
- 12+: only when the user asked for exhaustive coverage, and say the number first.

Batch when chunks are tiny. Ten one-line lookups is one `haiku` agent with a list of ten, not ten agents — spawn overhead would exceed the work.

If a job doesn't actually decompose — a single tangled refactor, a linear debug where each step depends on the last — say so and do it inline. Forcing a fan-out onto sequential work produces agents that duplicate each other and contradict each other at merge time.

## Writing the orders

A subagent sees none of this conversation. Its whole world is the prompt. Vague orders are the main reason fan-outs come back useless, and a cheap model punishes vagueness harder than an expensive one — so precision here is what makes rung 1 viable.

Each prompt should carry:

- **The concrete target** — absolute paths, exact symbols, specific URLs. Never "the config file".
- **The context you already scouted** — what the project is, what convention to follow, what you already ruled out.
- **A sharp scope boundary** — what NOT to touch. Without this, agents wander into each other's chunks and you get conflicting edits.
- **The return shape** — "return a list of `file:line — issue` lines, nothing else." The final text is the return value, not a message to a human. Specifying the shape means merging is concatenation instead of re-parsing.

Rule of thumb: if you could hand the prompt to a stranger with no other context and they'd do the right thing, it's ready.

## Write collisions

Parallel agents editing the same files clobber each other. Before dispatching any fleet that writes:

- Partition by file so no two agents touch the same path — this covers most cases and costs nothing.
- If they genuinely must touch overlapping files, pass `isolation: "worktree"` so each gets its own git worktree, then merge deliberately. Costs setup time and disk; only reach for it when partitioning is impossible.
- Read-only fleets need neither.

## Merging

Don't just staple the returns together — that's how contradictions reach the user as if they were findings.

- Drop duplicates where two agents found the same thing.
- When two agents disagree, resolve it: check the source yourself, or send one verifier at the appropriate rung. Don't report both.
- Note coverage gaps explicitly. If an agent returned null (user skipped it, or it died), say which chunk is uncovered rather than presenting partial coverage as complete.
- Report findings, not fleet logistics. The user cares what you found; agent count is a footnote.

## Escalation

If a `haiku` agent comes back confused, hedging, or obviously wrong, re-run that one chunk at the next rung — don't re-run the whole fleet. Cheap-first-then-escalate beats expensive-everywhere: the failure case costs one extra agent, and the common case saves the whole fleet.
