# tmux-pi plan

## Goal

One workspace where a main pi agent runs the work and every subagent is a real
pi or Claude Code session you can watch or take over. A local model decides which model and
effort each task gets, and frontier models receive only the context packet built
for their task.

## Layout

```
┌──────────────────────────────┬──────────────────────────────┐
│ main pi + subagents ext      │ status: subagents, model,    │
│                              │ state, current tool, cost    │
│                              ├──────────────────────────────┤
│                              │ viewer: selected subagent's  │
│                              │ live session (swapped in)    │
└──────────────────────────────┴──────────────────────────────┘
```

Each subagent runs in its own hidden tmux window. Viewing one swaps its pane
into the viewer slot, so what you see is the real session and you can type into it.

## Components

| Piece | What it does |
|---|---|
| `bin/tp` | Creates the tmux session and three slots, records pane ids as `@tp_*` session options. Linked as `agent`. |
| `bin/tp-pi` | Runs pi with only tmux-pi's config and skills. Used for main and pi workers. |
| `bin/tp-claude` | Runs Claude Code with only tmux-pi's settings, plugins and MCP servers. Used for Claude workers. |
| `agent/extensions/subagents.ts` | Loaded into main pi. Tools: `spawn_subagent`, `list_subagents`, `send_to_subagent`, `collect_result`, `kill_subagent`. |
| Router | `bin/tp-route` calls local Laya `typed-decisions` by default, applies a deterministic policy, and validates the result against `config/router.json`. The Slotstream OpenAI-compatible classifier remains an explicit fallback. Called by `spawn_subagent` when no model is given. |
| Context builder | `tp-spawn -c path[:start-end] -N note` writes `.tmux-pi/tasks/<id>/packet.md`: the task, then only the selected files or line ranges, then notes. It is the worker's first message; workers load no context files. `spawn_subagent` passes `files` and `notes` through. |
| Reporter | pi workers: `agent/extensions/reporter.ts`; Claude workers: hooks in the tmux-pi plugin (`claude/plugins/tmux-pi/hooks/`). Both write `.tmux-pi/tasks/<id>/state.json` (state, current tool, turns, tokens, cost) and `result.md` (last reply). `run.sh` writes `exit` when the process ends. |
| Status pane | `bin/tp-status`: live list from the task dirs and panes; keys to view, focus, read result/task, kill and archive. `enter` runs `tp-view <id>` (a tmux `swap-pane` into the viewer slot). |

## Self-contained config

tmux-pi never loads your personal pi or Claude Code setup. Everything a worker
can use lives in this repo:

```
skills/            shared by both harnesses (SKILL.md format)
agent/             pi agent dir (PI_CODING_AGENT_DIR)
  settings.json  models.json  extensions/  prompts/  themes/
  auth.json  sessions/                     (git-ignored; created by pi)
claude/            Claude Code worker config
  settings.json  mcp.json
  plugins/tmux-pi/ plugin; skills -> ../../../skills, hooks/ (reporter)
```

**pi (`bin/tp-pi`):** `PI_CODING_AGENT_DIR=agent/` replaces `~/.pi/agent`, and
`--no-skills --skill skills/` stops discovery of `~/.agents/skills` and project
skill dirs. Log in once inside tmux-pi (`/login`) to create `agent/auth.json`.

**Claude Code (`bin/tp-claude`):** `--setting-sources ""` skips user, project and
local settings, so no `~/.claude` plugins, skills, hooks or MCP servers, and no
project `.claude/` or CLAUDE.md. Settings, plugins and MCP servers come only from
`claude/` via `--settings`, `--plugin-dir` and `--strict-mcp-config --mcp-config`.
The normal Claude login is used, so the subscription works. Claude Code's
built-in skills (e.g. `code-review`, `simplify`) and built-in plugins
(`agents-md`, `telemetry`) still load.

Verified 2026-09-26 (pi 0.87.1, Claude Code 2.1.283): a probe skill in `skills/`
loaded in both harnesses; no user skills, plugins or MCP servers loaded; a
project CLAUDE.md was not read by the Claude worker.

The target project's `AGENTS.md` still reaches the main pi agent; workers skip
context files and get their packet instead.

State lives in `<workdir>/.tmux-pi/` (git-ignored). Tasks, packets, state and
results are plain files, so any piece can be inspected or replaced.

## Milestones

- **M0 — skeleton.** Repo, `bin/tp` (`agent`), three-slot layout, self-contained pi and Claude config. *(done)*
- **M1 — one subagent, by hand.** `tp-spawn --worker pi|claude --model X --thinking Y "task"` opens a hidden window running `tp-pi` or `tp-claude`; `tp-view <id>` swaps it into the viewer; `tp-list`, `tp-kill`. *(done)*
- **M2 — status.** Reporter extension plus a real status pane with keyboard selection. *(done)*
- **M3 — router.** Laya classifies bounded task traits; a deterministic ladder selects Slotstream, Codex, or Claude plus thinking level from a strict roster. Laya difficulty confidence is not used because its score temperatures are uncalibrated. `agent/extensions/subagents.ts` gives main the spawn/list/send/collect/kill tools and routes spawns without an explicit model. *(done)*
- **M4 — context packets.** Main builds per-task packets (task, selected files or line ranges, notes); subagents get nothing else. Size limits and a credentials-file denylist. *(done)*
- **M5 — review loop.** `tp-review <task-id>` and `review_subagent` start a non-editing reviewer on the other provider after a settled worker response (`waiting`) or successful exit. It receives the author task, packet, and result, independently checks the tree, and returns `APPROVED` or `REQUEST_CHANGES`. *(done)*
- **M6 — review-loop validation.** Cross-provider review was exercised end-to-end with a Pi author and Claude reviewer. `tp-review` accepts interactive workers in `waiting`, and `tp-claude-bootstrap [workdir]` provisions Claude folder trust once before workers run. *(done)*
- **M7 — review-policy gate.** Extend Laya's request schema with a bounded `task_intent` choice (`research`, `planning`, `implementation`, `configuration_or_operations`, `review`, `communication_or_documentation`). Validate its answer and combine it deterministically with difficulty, tools, private-context, sensitivity, risk, and ambiguity into `review: none|optional|required`. Require review for implementation/config/operations and consequential planning; skip ordinary research/summaries; escalate sensitive, high-risk, ambiguous, and user-requested review tasks. Make `review_subagent` consult the policy by default while preserving an explicit force-review path. Add fixture tests and a labeled calibration corpus before enabling automatic review handoff. *(done except automatic handoff: a learned layer over Laya's answers, trained and measured on a 300-task labeled corpus with a 120-task holdout, replaced the thresholds; see `tests/calibration/experiments.md`)*
- **M8 — Laya-directed dispatcher main.** `agent` remains the only launcher. For every user request, Laya’s bounded traits determine a validated model/effort route; deterministic policy separately chooses `placement: main|subagent` and review level. Brief, low-risk, non-review work runs in the main pane using Laya’s recommended model, which may be local or a frontier Pi model. Implementation, configuration/operations, sustained or multi-step work, consequential planning, and any required review run as subagents; required review automatically chains an author and cross-provider reviewer. Add bounded `execution_scope` (`brief|sustained|multi_step`) and `requires_project_changes` questions, policy fixtures/calibration rows, visible AUTO routing status, conservative Laya-outage fallback, Pi in-place model switching, and a same-workspace foreground-worker treatment for Claude routes (Pi cannot switch to Claude in place). User instructions to do work here, delegate, or review override placement/review but not roster validation. Definition of done: no normal-task manual routing/model choice; direct main tasks can use frontier Pi models; review-required tasks automatically receive cross-provider review; all flows remain in one tmux-pi workspace. *(done 2026-09-27)*
  - **Packets for dispatched work.** Building a packet does not need a strong model; the receiving model is the one doing the hard work. Every packet gets a deterministic core with no model involved: the verbatim request, the conversation since the last delegation (the one thing a worker cannot rediscover), a report-back template, and a standing instruction: *if the intent is unclear in a way where a wrong guess would change files or waste significant work, ask before acting; otherwise state your assumption and proceed.* A worker that asks settles in `waiting`; `collect_result` surfaces the question and `send_to_subagent` answers it. File selection is added only for cheap-tier workers, which explore poorly; strong workers (coding and above) get the core and read files themselves. Before relying on this, compare core-only, core + local file selection, and core + frontier file selection on a sample of real tasks by worker outcome.
  - **Decisions (2026-09-26).** Follow-ups: a message that refers to a running task goes to that worker (a Laya yes/no question with the active task as context); when unsure, ask the user. New traits: `execution_scope` is a new Laya question; the existing `changes` question serves as `requires_project_changes`; placement labels are added to the corpus, the traits feed the deterministic placement policy, and they join the learned layer only if they pass the tune/holdout gate. Overrides: explicit `+here`, `+delegate`, `+review` markers at the start of a message. Laya outage: stay in main on its current model, delegate nothing automatically, and show "AUTO off: Laya unavailable". Build order: phase 1 routing, placement, in-place main switching, status and fallback in a new `dispatcher.ts` extension; phase 2 automatic delegation with the packet core, follow-up routing and automatic review; phase 3 Claude routes as foreground workers.
  - **Phase 1 (done 2026-09-26).** `agent/extensions/dispatcher.ts` routes every main-agent message through `tp-route --dispatch` (one Laya request: route, learned intent, review, `execution_scope`, placement). Placement: change-making intents and required reviews always delegate; otherwise a learned placement head decides (0.81 on tune and holdout, no delegable task kept in main; experiments.md cycle 18). Main-placed pi routes switch the main model in place (skipped for the home model when the context would not fit); subagent placements return to the home model with a delegation note. Markers are `+here`, `+delegate`, `+review` (`@` is pi's file-reference syntax). `/auto on|off`; Laya outage leaves the model unchanged and shows "AUTO off: Laya unavailable".
  - **Phase 3 (done 2026-09-26).** A Claude route placed in main starts a foreground Claude worker (`tp-spawn --name fg --view`) with the ask-if-unclear note and the conversation since the last delegation; it is swapped into the viewer slot, its answer is posted into main, and later messages reach it through follow-up routing. If it cannot start, main answers on the home model. Verified live (a Haiku route forced through `local.json`: answer posted, follow-up judged and answered).
  - **Phase 2 (built 2026-09-26).** Subagent placements are spawned by the dispatcher itself (`tp-spawn --conversation`), so main takes no turn: the packet core is the verbatim request, the conversation since the last delegation, and the ask-if-unclear and report-back notes. A watcher follows each delegated task through escalation to its result, posted into main's conversation as a `tmux-pi-auto` message. Required review starts `tp-review` when the author reports; REQUEST_CHANGES goes back to the author once and is reviewed again (2 rounds at most). A reply ending in a question is shown as the worker's question and is not reviewed until the user answers. Follow-ups: `tp-route --follow-up` judges each running delegated task (at most the 3 most recent); the message goes to the one clear claimant, otherwise the user picks. Laya's score separates follow-ups too poorly to decide most messages (AUC 0.89; 22 of 56 decided), so a machine with a fast local chat model uses it instead (`followup` in `config/local.json`; Qwen3.5-4B: 47 of 56 decided, 0 wrong, 0.18 s; experiments.md). Follow-ups are saved to the task's `followups.md`, which its reviewer reads. Verified end to end in a scratch workspace: delegation, approval, a follow-up, a REQUEST_CHANGES round, and a separate question kept in main. A `+followup [id]` marker skips the judge. Claude workers stopped at a choice prompt (folder trust, tool permission) are relayed to main as a dialog and the pick is keyed in; verified live with both prompts.
  - **Packet file selection (done 2026-09-27).** Compared on 30 chores that do not say where the answer is (experiments.md): Qwen3.5-4B workers went from 44/60 with the core only to 50/60 with locally picked files and 53/60 with files picked by Sonnet 5; Slotstream was 28/30 in all three and only traded tool calls for prompt tokens. The local model picked the right file less often than assumed (18–20 of 30, against 24 for Haiku and 26 for Sonnet 5, about 2 s per pick), so the selector is a Claude model, not the local one. Models listed in `packet_files` in `config/local.json` get up to 3 picked files (`tp-route --pick-files`; files over 16,000 characters are cut to a line range, credentials names are never listed); every other worker gets the core only. If picking fails the worker starts with the core.

- **M9 — bounded research and orchestration controls.** Apply the lessons from the 2026-09-26 AgentThoughts research run before adding more skills or autonomous workflows. The purpose is to make agent count, model tier, elapsed time, and output quality explicit control surfaces rather than assuming that more parallel agents produce more progress.
  - **External-research model floor.** Requests requiring current external information, multi-source comparison, citations, or broad synthesis default to a smaller frontier model or better. Local models are not the default researchers. They remain eligible for bounded, verifiable work: internal repository inventory, extraction to a fixed schema, deterministic retrieval/reranking candidates, routing, and narrow first-pass review. This is a proposed policy based on one observed run; validate it on matched prompts rather than treating it as a permanent model verdict.
  - **Research contract.** Every research worker gets one bounded question, a destination artifact, preferred source types, a time/turn/token budget, and an early deliverable (source table or outline). It must write a partial artifact before broad exploration. Source facts, inference, and hypotheses are labeled separately. A report without source URLs/paths and access date is not complete.
  - **Output-time checkpoints.** At a short checkpoint the worker must have written an outline plus initial sources; at the main checkpoint it must have a usable draft; at the hard limit it must stop, save the partial report, state missing evidence, and ask whether to continue, escalate, narrow, or discard. The exact thresholds are configuration/experiment inputs, not hidden model behavior. Status must expose elapsed wall time, model, turns/tokens/cost where available, last artifact-write time, and checkpoint state.
  - **Parallelism limits.** Default to one research worker. Permit at most 3 concurrent external-research workers and at most 4 total active workers across the workspace, including review. Launch another only when its question and evidence source are materially independent. A synthesis/reviewer waits until its inputs are settled; do not launch a large fan-out plus synthesis simultaneously. The human can override the limits deliberately.
  - **Circuit breakers and escalation.** Stop or pause a worker that misses its first artifact checkpoint, repeats tool/search activity without adding evidence, exceeds its budget, or cannot access needed sources. Preserve its task directory and partial report. Escalate by narrowing the question or changing to a stronger model; do not automatically retry the same open-ended prompt on more local workers.
  - **Completion-state clarity.** Do not overload `waiting`. The status surface must distinguish: `working` (still producing), `needs_input` (a human decision/permission is blocking), `settled` (a response/artifact is ready for `collect_result` or review), `completed` (the result was collected/handed off), `paused`, `failed`, and `killed`. Show the artifact path and last result summary for settled work. This is required so a completed worker never appears to have silently lost its answer.
  - **Cross-provider review.** For consequential research/design decisions, use a second provider after the report exists to check primary-source support, hidden assumptions, applicability to this workflow, and whether local measurements contradict desk research. The reviewer should identify claims as verified, report-derived, background knowledge, or synthesis.
  - **Measured adoption.** Record each run in a compact experiment ledger: task class, model/tier, provider, requested/output artifact, elapsed time, token/cost if available, source validity, human cleanup, result usefulness, and stop/escalation reason. Compare local versus smaller-frontier research on 3–5 matched prompts before changing routing defaults. Use actual tmux-pi work and manual overrides as calibration data; do not rely only on synthetic corpora.
  - **Capability-adaptive policy.** tmux-pi must route by a versioned capability profile—not a permanent belief that one vendor, frontier model, local model, MCP, or method is inherently best. Express needs such as external research, bounded editing, visual validation, read-only retrieval, independent review, privacy/offline operation, and structured extraction. Record model/tool/provider versions and qualification evidence; re-evaluate routes when capability, cost, latency, safety, or reliability materially changes. Keep substitutions/configuration rollbackable. The former `.agents` and `my-claude-skills` projects remain reference material only; do not recreate or make tmux-pi dependent on either system.
  - **Candidate initial-capability selector.** Evaluate whether Laya can classify the task contract into a small set of initial skill/MCP/tool categories for progressive disclosure. It should return bounded categories, not decide authority or invent tool use. Compare it with deterministic task-to-capability rules on qualification fixtures; keep only if it improves initial selection without hiding needed capabilities, leaking context, or creating additional escalation/friction. Models may still request further policy-checked capability through the registry.
  - **Initial implementation slices.** (1) Add optional contract/budget/checkpoint fields to task metadata and status rendering, without changing dispatch defaults. (2) Add a manual research route that enforces artifact/checkpoint reporting. (3) Collect a small real-task ledger and evaluate outcomes. (4) Only if the data supports it, add conservative router policy for research model floors and parallel caps. No generic RAG, broad always-on memory, or large skill pack is in scope for M9.
  - **Slices 1–2 (done 2026-09-27).** Contracts are optional metadata (`meta.json` `contract`: kind, artifact, budget, outline/draft checkpoints, stall, grace), set by `tp-spawn -k/-o/-b/-p`; dispatch defaults are unchanged. `tp-contract enforce`, run by the dispatcher every 15 s, types one reminder per missed checkpoint or stall, asks the worker to save a partial report and ask how to go on when the budget is used up, and pauses (Escape) a worker that misses its first checkpoint by the grace period or keeps working after the stop; actions go to `checkpoints.json` and into main. The status pane shows elapsed time, turns, checkpoint state and last artifact write. `+research [+review] <question>` starts a Claude Code Sonnet worker (medium thinking; `config/research.json`) with the research contract and labeling rules; reports go to `.tmux-pi/research/<id>.md`; parallel limits were first a confirm before exceeding 3 research or 4 active workers (replaced by the queue, item 5). A research review checks and classifies each key claim. Verified live: reminders, stop and pause on local Qwen workers (Qwen answered the stop but did not save `Status: partial`); a web research run (5 min, outline on time, 4 source URLs, honest about no official doc); a reviewed run whose reviewer caught access dates given for sources the worker never fetched, after which the note separates background knowledge from fetched sources. The completion states, halted-task decisions and the research queue followed (items 1–5 below).
  - **Slice 3 (started 2026-09-27).** `tp-ledger` records each settled research task (`.tmux-pi/ledger.jsonl`: model, provider, time, turns, tokens, artifact status, source URLs, checkpoint actions, stop reason) and `tp-ledger rate` adds sources/cleanup/usefulness. Two rows so far. Next: real research tasks, plus 3–5 matched prompts on a local model (`tp-spawn -k research -m <local>`; no web tools, so only for questions answerable from the repo) against Sonnet.
  - **Slice 4 (waiting on slice 3).** No router floor or cap changes until the ledger supports them.
  - **Remaining (planned 2026-09-27), in order.**
    1. *Completion states.* Done 2026-09-27: `lib/contract.py` `lifecycle` derives `starting`, `working`, `needs_input` (permission or a reply ending in a question), `paused`, `settled`, `completed`, `failed`, `killed` and `escalated`; `tp-contract status` feeds tp-status and tp-list (state, tool calls, a one-line summary); the dispatcher marks a task `completed` when it posts the result, and `collect_result` does through `tp-contract collect`. Only `starting`/`working`/`needs_input` count toward the caps. Completed and paused compare against the reply's time, since Claude touches state.json again on its idle notification.
    2. *Stop outcomes.* Done 2026-09-27: `enforce` records every contract task (research or not) in the ledger once per settled state; the stop reason says `saved partial` or `no partial saved` from the artifact's status, and main's halted post says the same.
    3. *Circuit breakers.* Done 2026-09-27: the stall breaker (no artifact writes for the stall limit while working; this covers searching without new evidence) and a failed-tool-calls breaker (`tool_errors` in a row, counted by the pi reporter and the Claude hooks) each give one reminder, then a pause after the grace period. Nothing is typed into a paused worker. Tested on a Claude worker: the pause interrupts it, its input box is left empty, and since Claude reports no Stop on an interrupt, a paused worker whose tool count has not moved counts as idle.
    4. *Escalation from main.* Done 2026-09-27: a stopped or paused task that is no longer working is `halted`; main posts why, whether a partial was saved, the reply and the report path, then offers continue (one budget extension, `tp-contract extend`), narrow or escalate (research: a new task on a narrower question, or on the `escalate` route, told to build on the partial report), discard, or decide later. Nothing is retried automatically. Continue, narrow and discard tested live on Claude workers.
    5. *Parallelism rules* (revised by the user 2026-09-27: one local research at a time, independent frontier research in parallel up to 3, dependent research waits). Done 2026-09-27: `tp-contract slot` checks dependencies, then `max_local` (1), `max_frontier` (3) and `max_active` (4); a request that cannot start goes into `.tmux-pi/research-queue.json` and starts from the 15-second loop, with the reports it builds on in its notes. Dependencies come from `+after <id>[,<id>]` or, while research is running, the follow-up judge. Queueing, a dependent start and an independent question judged independent were tested live.
    6. *Ledger for evaluation.* Add task class, the requested artifact, the harness and model version (Claude Code / pi version, model id) and tool availability (web or not) to each row. Run 3–5 matched repo-answerable questions on a local model and on Sonnet (`tp-spawn -k research -m …`), plus the real research tasks that come up, and rate them.
    7. *spawn_subagent contracts.* Give `spawn_subagent` in `agent/extensions/subagents.ts` optional `kind`, `artifact`, `budget` and `checkpoints` parameters passed to `tp-spawn`, so main can start contract work itself.
    8. *Slice 4, only if the data supports it.* A research-intent route floor (external research never goes to a local model) and caps in router policy, versioned like the rest of the roster and easy to roll back. The capability-profile idea (routing by needs such as web access, not by vendor) starts here as roster fields (`web`, `offline`, `review`), not a new system.

## Models available now

- **Local (slotstream):** Qwen3.8 Flash Next, OpenAI-compatible, on `--port 11435` (what `agent/models.json` expects). Rejects the `store` field, so `compat.supportsStore` is false.
- **Local (Laya router):** `typed-decisions` through `POST /v1/systemone` on loopback port `11436`; repo-local runtime artifacts stay under ignored `.laya/`.
- **GPT:** pi's `openai-codex` provider (ChatGPT subscription); needs `/login` inside tmux-pi.
- **Claude:** Claude Code workers via `bin/tp-claude`, on the Claude subscription.

## Decisions

- Private GitHub repo `vandiverj/tmux-pi`.
- pi for the main agent and default workers; Claude Code as a worker type, so Claude runs on the subscription in Anthropic's own client.
- Self-contained: all config, extensions, plugins and skills live in this repo; nothing from `~/.pi`, `~/.agents` or `~/.claude`.
- TypeScript for extensions and tools; bash for thin tmux wrappers.
- Ollama is not used; slotstream is the only local provider.
- Started clean rather than extending `pi-improvement-workbench`; borrow from it only where it helps.

## Open questions

- **Router calibration:** The corpus labels are one person's judgement; real tmux-pi tasks labeled by the user would be the best next data. Route accuracy is limited by Laya's answers (55% tier, 66% model on holdout); under-tiering can be traded for over-tiering with a cost weight, which is a quality-versus-spend choice.
- **Main agent model:** local or frontier. Recommended: frontier at low thinking for main, since it writes every packet.

## Risks

- **`openai-codex/gpt-5.3-codex-spark` fails on a ChatGPT login** ("not supported when using Codex with a ChatGPT account", 2026-09-26). It is in the router roster but no route targets it; `gpt-5.5` works.
- **Packets are only as good as their file selection.** The picker missed the needed file on 4 of 30 chores; workers can still read files themselves, which costs extra turns. Picking sends the workdir's file names (not contents) to the selector model. Weaker models also re-read files that are already in the packet.
- **Claude built-ins can't be removed.** `--disable-slash-commands` would drop them but also drops tmux-pi's skills. Denying specific ones via `claude/settings.json` is untested.
- **Claude workers stop at the folder-trust prompt** the first time they run in an untrusted directory; main shows it as a dialog (the relay reads Claude's screen, so a changed prompt layout would fall back to the "has not started working" notice).
- **Claude token totals lag one turn at Stop.** The transcript can lack the final message when the Stop hook runs; totals catch up on the next prompt.
- **tmux server options.** `tp` turns on `extended-keys` (csi-u) server-wide, which also affects any other tmux sessions.
