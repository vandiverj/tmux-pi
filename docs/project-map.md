# tmux-pi: project map (context prompt for a redefinition session)

## How to use this
I am redefining what this project is. Below is an inventory of what exists today (as of 2026-09-30, 30 commits over 2026-09-26 to 2026-09-30, ~13.5k lines of code and config, 107 tracked files). Treat it as ground truth about the current state, not as a design to preserve. Help me decide what the core product is, what to keep, what to cut or extract, and what to rebuild. Ask me questions before proposing a direction. Where I mark something as an observation, it is my read, not a fact from the docs.

## 1. One-paragraph summary
tmux-pi is a personal tmux workspace (launched with `bin/tp`, symlinked as `agent`) that runs a "main" [pi](https://github.com/earendil-works/pi) coding agent alongside subagents. Each subagent is a real pi or Claude Code session in its own hidden tmux window that can be watched or typed into. Layout: main pane on the left, a live status pane top-right, and a viewer pane bottom-right that swaps in the selected subagent's live session. Around that core, the project grew a large control plane: a local-model router (Laya) that picks model and effort per task, context packets, cross-provider review, task contracts and budgets, escalation, research orchestration, a capability registry, goal mode, retention and cleanup, routing evals, and a selective skill catalogue. Everything is self-contained (it never loads `~/.pi`, `~/.claude`, or `~/.agents`). It is one person's tool, on macOS, tied to specific local models and subscriptions.

## 2. Timeline (how the shape emerged)
| Date | What landed |
|---|---|
| 09-26 | M0 tmux launcher and self-contained pi/Claude config; M1 spawn/view/list/kill; M2 reporters and status pane; M3 Laya router and main-agent subagent tools; M4 context packets; M5/M6 cross-provider review; M7 learned calibration layer over Laya (300-task labeled corpus); M8 dispatcher (auto-routing and placement, auto-delegation, follow-up routing, foreground Claude workers) |
| 09-27 | Packet file selection; M9 research contracts, checkpoints, circuit breakers, halted-task decisions, research queue, run ledger; `/end` and `tp-exit` |
| 09-28 | "Process alignment" slices 1-6: canonical task records, preflight, context broker, capability registry, automation gating, retention and cleanup, R5 rewrite of the dispatcher into a contract-handshake gate; Qwen3.6-35B on LM Studio; make-qa skill; review lifecycle |
| 09-29 | Selective skill catalogue; on-demand SDLC guidance profiles (counterpoint, product-fit, threat-model) |
| 09-30 | Goal mode, routing evals, and the **local assistant mode** (`ask_local`), which became the default; strict dispatcher is now opt-in; `tp-improve` friction report; Claude worker permission pre-approvals; pane repair on reattach |

Observation: the trajectory is heavy governance and automation (09-28) followed by a pivot on 09-30 to a lighter default (local assistant plus guardrails, with the contract system demoted to `"mode": "strict"`). The project may already be mid-redefinition.

## 3. Architecture at a glance

```
bin/tp  ──►  tmux session "tmux-pi"
             ├─ main pane:   bin/tp-pi  (pi, PI_CODING_AGENT_DIR=agent/, extensions below)
             ├─ status pane: bin/tp-status (live list, keys: j/k/enter/o/r/t/x/c)
             └─ viewer pane: swap-pane target for a subagent's live window

subagent = hidden tmux window running run.sh → bin/tp-pi (pi worker) or bin/tp-claude (Claude Code worker)
state    = <workdir>/.tmux-pi/  (plain files: tasks/<id>/{task.md, request.md, packet.md, meta.json,
           state.json, result.md, context-manifest.json, checkpoints.json, ...}, handoffs/, goals/,
           evals/, research/, ledger.jsonl, cleanup.log, archive/)
reporting = pi: agent/extensions/reporter.ts;  Claude: plugin hooks (claude/plugins/tmux-pi/hooks/report.sh)
routing   = Laya typed-decisions (local HTTP service, port 11436) → learned logistic-regression heads
            (config/laya-model.json) → route table (config/router.json) → roster-validated model + thinking
```

Design constants: state is plain files so any piece can be inspected or replaced; TypeScript for pi extensions, Python for logic (`lib/`), bash for thin tmux wrappers; tools are cooperative guardrails, explicitly not an OS sandbox.

## 4. Component inventory

### 4a. Workspace and worker lifecycle (the original core)
| Piece | Role |
|---|---|
| `bin/tp` (+ `lib/tp.sh`) | Create/reattach the tmux session and three slots; record pane ids as `@tp_*` session options; repair missing panes on reattach. Runs `tp-lmstudio` at launch. |
| `bin/tp-pi`, `bin/tp-claude`, `bin/tp-claude-bootstrap` | Self-contained harness launchers. pi: only this repo's `agent/` dir and `skills/`. Claude: `--setting-sources ""` plus repo `claude/` settings, plugin and MCP config only; bootstrap handles one-time folder trust. |
| `bin/tp-spawn` (402 lines) | Build a packet and `run.sh`, open a hidden window, start the worker. Flags for worker, model, thinking, context files (`-c path[:a-b]`), notes, workdir, contract (`-k -o -b -p`), view. |
| `tp-list`, `tp-view`, `tp-kill`, `tp-clean`, `tp-reset`, `tp-exit` | List, swap into viewer, stop, archive killed, stop all and reset, and end the workspace (with retention). |
| `bin/tp-status` | Interactive live status pane (repaints only on change). |
| Reporters | Write `state.json` (state, current tool, turns, tokens, cost) and `result.md` (last reply). Worker states: starting, working, tool, waiting, permission, error, done, failed, killed. |
| Context packets | The worker's first message: task, selected files or line ranges, notes. Workers load no AGENTS.md or CLAUDE.md. Size limits and a credential-file denylist. |

### 4b. Main-agent extensions (`agent/extensions/`, loaded into main pi)
| File | Lines | Role |
|---|---|---|
| `dispatcher.ts` | ~2050 | The largest file. Strict mode: routes every message through `tp-route --dispatch`, persists a non-authorizing handshake scaffold, forces an Orient/Frame contract via `tmux_pi_commit_contract`, evaluates policy, dispatches workers at `agent_end`, follows delegated tasks, posts results into main as `tmux-pi-auto` messages, runs review chains, follow-up routing, capability requests, contract enforcement every 15s, halted-task dialogs, research queue, `+here/+delegate/+review/+plan/+prototype/+investigate/+research/+followup` markers, `/auto on|off`. |
| `assistant.ts` | ~120 | **Default mode.** `ask_local` hands read-only search and read jobs to a fresh local model (LM Studio Qwen) that returns paths, line numbers and snippets. Also blocks destructive shell commands and edits outside the workdir. |
| `subagents.ts` | ~340 | Tools for main: `spawn_subagent`, `list_subagents`, `send_to_subagent`, `collect_result`, `kill_subagent`, `review_subagent`; `/reset`, `/end`. |
| `escalate.ts` | ~280 | Worker-side one-step escalation after consecutive tool errors. |
| `goal.ts` | ~220 | Opt-in bounded continuation toward an approved objective (`/goal attach`). |
| `reporter.ts` | ~150 | pi worker state reporting. |
| `catalogue.ts` | ~80 | Selective skill injection (at most 3 matched skills per turn) and the `tmux_pi_find_skill` tool. |

### 4c. Routing and evaluation
| Piece | Role |
|---|---|
| `bin/tp-route`, `lib/router.py` (847 lines) | Ask Laya six bounded questions (intent, difficulty, needs tools, sensitive, high-risk, changes things); learned heads pick route, intent, review level, execution scope and placement. Output is only `worker`, `model`, `thinking`. Modes: `--dispatch`, `--review`, `--follow-up`, `--pick-files`, `--check`. |
| `bin/tp-laya` | Start and stop the local Laya service (pinned commit, loopback only, offline). |
| `config/router.json` | Roster (9 models: local Qwen variants, GPT-6 Luna/Sol/Astra, Claude Haiku/Sonnet/Opus), 5 tiers (simple, standard, coding, complex, expert), and `claude_preference` (Sonnet default, Opus for heavy). |
| `config/laya-model.json` (696 lines) | Learned multinomial-logistic layer, trained on 180 tune tasks; measured on 120 holdout tasks (about 55% tier match, 66% model match). |
| `bin/tp-calibrate`, `lib/calibration.py`, `tests/calibration/*` | 300-task labeled corpus, recorded Laya answers, experiments log. `--fit` needs numpy. |
| `bin/tp-evals`, `lib/routing_evals.py` | Routing selection logs, human outcome ratings, joined exports; no text logged by default. |
| `bin/tp-improve` | Mechanical (no-model) friction report over pi session transcripts: compaction storms, re-reads, main-model work the local assistant could do, guard blocks, repeated errors, cost per model. |
| Local model plumbing | `bin/tp-lmstudio` (keeps Qwen3.6-35B-A3B loaded), `bin/tp-mlx` (optional mlx-lm server), `config/local.json` (git-ignored, per-machine routes, escalation, rules, prompts, follow-up judge, packet_files, assistant, mode). |

### 4d. Governance and control plane ("process alignment" slices 1-6, M9)
| Piece | Role |
|---|---|
| `bin/tp-task`, `lib/task_record.py` (890 lines) | Canonical human `task.md` per task, list/resume, review-list/review-resolve, cleanup dry-run and reviewed deletion, main-contract, automation and capability CLIs. |
| `lib/preflight.py` | Slice 2 preflight: task mode, frame, assumptions, allowed paths/tools, decision boundary, proof path, artifact tier; durable no-edit stop records for `+plan/+prototype/+investigate`. |
| `lib/context_broker.py` | Slice 3 provenance-aware context manifest; retrieved text is recorded as untrusted evidence. |
| `lib/capabilities.py` (586 lines) | Slice 5 versioned capability registry with progressive disclosure. Profile-only stubs (YouTrack, Context Engine, read-only DB, official docs, browser validation) are documented but inactive. |
| `lib/automation.py` | Slice 6 automation gating. **Both automatic cells are currently pending or disabled**, so unqualified work becomes a recommendation or manual status. |
| `bin/tp-contract`, `lib/contract.py` (677 lines) | Budgets, checkpoints, stall and tool-error circuit breakers, lifecycle states (starting, working, needs_input, paused, settled, completed, failed, killed, escalated), extend, slot/parallelism limits, research queue. |
| `bin/tp-review` | Cross-provider read-only reviewer (Pi author gets Claude Sonnet, Claude author gets Pi/Codex); review policy from the learned classifier; `APPROVED` or `REQUEST_CHANGES`; one automatic revision round. |
| `bin/tp-ledger` | Research and contract run ledger with human ratings. |
| `bin/tp-goal`, `lib/goal.py` (709 lines) | Approval-gated, budgeted goal records; the human-only start needs `--confirm START-GOAL`. |
| Retention | `/end` archives instead of deleting; `--delete-reviewed ... --confirm DELETE-ELIGIBLE-UNPINNED`; pins, ticket-linked, plan/prototype, and durable records are protected. |

### 4e. Skills and prompts
- `skills/make-qa/` (tester-facing QA script drafting, invoked as `/make-qa`); `catalog/skills/{counterpoint, product-fit, threat-model, document-transform, test-skill-behavior}`; `config/skill-catalog.json` (private trigger profiles); `agent/prompts/*.md` (slash-prompt aliases); `bin/tp-skills` (deterministic selector).
- Design principle: skills are not globally advertised. A matched skill's body is injected for that turn only (max 3), so unrelated turns carry no skill descriptions.

### 4f. Claude worker config
`claude/settings.json` (pre-approved read/test Bash commands, denied destructive commands), `claude/mcp.json`, and `claude/plugins/tmux-pi/` (hooks that write reporter state).

## 5. Feature surface by user-visible concept
- **Delegation:** spawn, list, send, collect, kill, review; view any worker live and type into it.
- **Routing:** the local model chooses model and thinking per task; the roster is strictly validated; the main model is not switched in place (removed in R5).
- **Two modes:** default is *assistant* (main thinks, local model does read-only legwork, cheap guardrails); *strict* is the full contract, preflight, policy, and review handshake.
- **Markers:** `+here +delegate +review +plan +prototype +investigate +research +followup +after`.
- **Research:** bounded research workers with artifacts, checkpoints, budgets, parallel caps, queueing, and a ledger.
- **Review:** required, optional, or none, decided by a learned policy; opposite-provider only.
- **Goals:** opt-in bounded continuation with human approval, budgets, and mandatory review.
- **Retention:** durable task records, archive on end, review-first cleanup.
- **Measurement:** calibration corpus, routing evals, `tp-improve`.

## 6. External dependencies and assumptions
tmux 3.3+, macOS (uses a macOS-compatible advisory lock), pi (0.87.1 verified), Claude Code (2.1.283 verified), a Claude subscription, ChatGPT/Codex subscription via pi `/login` (GPT-6 models), LM Studio with Qwen3.6-35B-A3B (about 20 GB), Slotstream (local Qwen3.8 Flash Next on port 11435), Laya (cloned into `.laya/`, pinned to a commit, Python 3.10+, torch), `jq`, Python 3 (numpy only for `--fit`), Node with `--experimental-strip-types` for TS runtime tests. GitHub private repo `vandiverj/tmux-pi`. Model IDs and prices must be rechecked before relying on the roster.

## 7. Quality and test posture
- 27 files in `tests/`: about 22 Python unittest modules plus Node runtime harnesses and a pinned TypeScript 5.9.3 type check.
- Tests use fake child-process and tmux boundaries, pinned router decisions, and real policy/CLI logic. Docs state plainly that they are **not** live model-quality qualification, not a live Pi session, and not an OS sandbox.
- No automation cell is qualified or enabled. Calibration labels are one person's judgment.

## 8. Known limitations and open items (from the project's own docs)
- Guardrails are cooperative and pattern-based, not an OS sandbox (bash, alternate tool names, unrestricted post-contract shells).
- Follow-ups typed into a worker are not re-policied.
- Dispatcher restart does not reattach to active delegated tasks; the in-memory watcher can be lost.
- Router accuracy is capped by Laya's answers (about 55% tier match); under-tiering versus over-tiering is a spend/quality tradeoff.
- Local-model research quality was observed on essentially one run; the floor policy is unvalidated. M9 slice 4 (router floors and caps) waits on ledger data that is thin (two rows).
- Cross-provider escalation targets are not fixture-qualified.
- Packet file selection misses the needed file about 4 in 30 times.
- Claude built-in skills cannot be removed; Claude workers stop at the folder-trust prompt (relayed to main as a dialog).
- The README is 536 lines and largely a change log of guardrail semantics (one paragraph is over 1,500 words); the design has outgrown its docs.

## 9. My observations (challenge these)
1. **Three products are entangled.** (a) A tmux multi-agent cockpit with watchable subagents (about 1k lines of shell plus reporters, and the most novel and concrete part). (b) A model-router/evaluation stack (Laya, learned heads, calibration corpus, routing evals). (c) A governance/process engine (handshake, preflight, capability registry, automation cells, retention, review-resolve ceremonies, goal mode). They have different users, cost profiles and maintenance burdens.
2. **The heaviest layer appears least used.** The strict-mode dispatcher (about 2k lines) and the automation policy have no enabled automation cells. The 09-30 pivot made the light assistant mode the default.
3. **The newest idea is the simplest.** `ask_local` (main thinks, local model does read-only legwork with fresh context) plus a friction report (`tp-improve`) is about 275 lines and targets a concrete pain (main-model compaction and token cost).
4. **Heavy machine coupling.** The roster, Laya, LM Studio, and Slotstream assume one person's hardware and subscriptions. The pi-specific extension API ties the main agent to pi.
5. **Sunk-cost risk lives in the governance layer.** It encodes many hard-won safety lessons (review binding, no waiver-by-deletion, fail-closed defaults), which are worth preserving as principles even if the code shrinks.

## 10. Questions I want the session to help me answer
1. What is the one-sentence product? Candidates: (A) a watchable multi-agent cockpit; (B) a cost-aware local/frontier model orchestrator; (C) a governed agent-delegation framework with audit trails; (D) something else.
2. Who is it for: only me, my team, or open source? That decides how much configurability and safety ceremony is worth it.
3. Which components are core, which should be extracted into standalone tools (router and evals, `tp-improve`, the skill catalogue), which should be frozen (strict mode, capability stubs), and which should be deleted?
4. Should the main agent stay pi, move to Claude Code, or become harness-agnostic?
5. Does governance belong inside this tool (hard gates) or in the harness and human process (light guardrails)? What is the minimum that keeps the cross-provider review value?
6. What evidence would justify keeping the learned router (currently about 55% tier match), versus simple rules or an explicit user pick?
7. What should the documentation and mental model look like after redefinition (for example a 1-page README plus separate reference docs)?

## 11. Key files to read first
`README.md`, `docs/plan.md` (milestones and decisions), `agent/extensions/assistant.ts`, `agent/extensions/subagents.ts`, `bin/tp`, `bin/tp-spawn`, `lib/router.py`, `config/router.json`, `config/local.example.json`, `docs/goal-mode.md`, `docs/routing-evals.md`, `tests/calibration/experiments.md`.
