# HUB: dependable personal development hub (owner workstream, 2026-11-11)

Owner request (verbatim): `owner-request.md`. Target: "I provide a goal, correct the process once if necessary, leave and return,
and receive current completion evidence without repeatedly reminding Main how to operate." Correct outcomes, fewer repeated
reminders, reliable recovery and less supervision come first; token savings second. Built and evaluated on this Mac; the work Mac
gets a handoff (HB9) and only validates environment differences, work-only integrations and real use.
Queue rows: `../loop/ideas.md` H35-H42. Status: **HB1 audit done; HB0 interfaces drafted; lanes hub-correct (HB4a, `hb4/hb4a-brief.md`) and hub-episodes (HB8a, `hb8/hb8a-brief.md`) started 2026-11-11.**

## Baseline (HB1, read-only audit of main 6c8094f = 451a5e1 + origin's 5 gate commits)
Reports: `audit/A-instructions.md` (launch routes, every read/search statement), `audit/B-control.md` (message, interrupt,
request identity, telemetry), `audit/C-memory-evidence.md` (stores, evidence records, transcript failures). Main re-checked the
starred lines in source. OBS = observed in real runs, SRC = confirmed in source, HYP = hypothesis.

| # | Lead / finding | Now | Evidence |
|---|---|---|---|
| 1 | coder.md recommends `grep -rn` | SRC, still on main* | `agent/roles/coder.md:21`; the only search line a production worker gets. SG1 lane changes it (unmerged) but uses `safe_grep` in coder.md and `mcp__tmux-pi-safe__safe_grep` in its tp-spawn block; tool descriptions differ by harness (pi: "outside the working tree", Claude: "files Read/Grep refuses") |
| 2 | Tools block (with standing lines) dropped without a manifest | SRC* | `lib/agent_tools.py:26,41-45,195-197`, `lib/preamble.py:94-96`; tmux-pi has no `.tmux-pi/agent-tools.json`, so production workers never get "use Read or safe_read" or the retry prior-result line. Bench base commits a manifest (`lib/bench/project.py:14,36`): **bench workers get different instructions from production** |
| 3 | Decision recording missing from Main's prompt | SRC | `docs/persistent-work.md:93-101` in no orchestrator/main prompt; work store unused: 0 workspaces, 52 compactions, 0 `work checkpoint` (OBS) |
| 4 | Main gets generic prompt; project context goes to workers | SRC | `bin/tp-main:145-155`; preamble/manifest read only by tp-spawn; ctx tools need a task (`bin/tp-claude:47-49`, `bin/tp-pi:69-73`) |
| 5 | No cancel-and-redirect | SRC* | `bin/tp-agent:1716-1748` never reads worker state or sends Escape; text + Enters land in a permission dialog; busy Claude → "unconfirmed" → "send again" → duplicate; queued input-box text not cleared |
| 6a | Pause recorded before the interrupt | SRC* | `bin/tp-contract:254-268`; pause inferred from an unmoving tool count (`lib/contract.py:404-423`); only the pi Main runs the watchdog |
| 6b | Notification identity is task/status | SRC | `claude/plugins/tmux-pi/hooks/register.tsx:12,225-256`; `capability-request.json` overwritten and marked answered by any confirmed message (`bin/tp-agent:1664,1730-1735`); `permission-request.json` pi-only, no id |
| 7 | tp-bench drives workers, not Main | SRC | `lib/bench/runner.py:287-362`; counts "paused" as finished (`:25`) |
| 8 | Telemetry differs by harness and tool name | partly SRC | reporters log names as given; readers differ: `lib/routines.py:121` misses safe tools, `lib/trial_scan.py:893-929` Claude names only, skips pi; pi has no permission state; Claude `PermissionDenied` not routed (`hooks.json:8`) |
| N1 | Reviewers never get `agent/roles/code-reviewer.md` | SRC* | `bin/tp-review:482` passes `--preamble-role` without `--role`; `bin/tp-spawn:546` adds role text only with `--role` |
| N2 | Native Agent-tool children get no standing text | SRC | Main's children inherit `TP_MODE=orchestrator` without `TP_TASK_DIR`; workers' children get no packet |
| N3 | Launch package only partly recorded | SRC | task dir keeps packet.md, run.sh, meta.json, dispatch.json; no commit/role-file hashes, exposed MCP tools, tp-claude suffix rule, Main's own package |
| O1 | Workers search with shell grep/sed/cat | OBS | 8 sampled coders: 11-30 grep, 4-27 sed calls each; only SG1 r1 used safe_grep; 0 packets mention safe_grep |
| O2 | ~30 permission prompts, each a Main redirect | OBS | 12 recursive search of config/agent/claude, 6 `.env` text, 5 unscoped git diff, 3 named sensitive file |
| O3 | Messages sent into prompts / lost / clobbering results | OBS | coder-62fe70c08d messaged in permission state, ended without result; 3 results overwritten by an ack (H25); 7 results without RESULT_STATUS |
| O4 | Corrections and decisions travel as messages + `fixes/*.md` | OBS | 7 brief corrections and 8 ownership grants by message; none recorded as a decision |
| O5 | Owner process reminders | OBS | 8 owner corrections quoted in `audit/C-memory-evidence.md` F6 (e.g. 2026-11-10T14:55Z safe_grep; 2026-11-11T03:20Z tools are for target projects) |
| O6 | Verification duplicated, evidence unchecked | OBS/SRC | 0 of 416 tasks have gate.json; worker and Main re-ran the same 4 cells per SG1 round; lane close checks only the reviewed tree (`lib/lane_steps.py:299-387`) |

Versions on this Mac: Claude Code 2.1.322, pi 1.1.0, tmux 3.7c, system python 3.9.6 (hooks), node 24.

## Items (each: problem, hypothesis, dependencies, scope, benefit, evaluation, evidence, decision)
Priority order from the owner: HB2, HB3, HB4 with HB8 alongside; then HB5, HB6, HB7. HB0 first because they share interfaces.

### HB0 shared interfaces (Main, before any lane) — `interfaces.md`
Launch record, universal contract file, event journal line, request identity, decision/preference record, obligation record.
One schema each, written once; lanes implement against it. Decision: adopt when the owner has seen it with HB2-HB4's first briefs.

### HB2 instruction and capability delivery (H35)
- **Problem:** findings 1, 2, 4, N1, N2, N3, O1, O2. **Hypothesis:** one always-delivered contract plus a launch record removes most
  tool-choice reminders and makes any miss explainable.
- **Scope:** `agent/contract/worker.md` (universal contract, versioned, ~15 lines: scope, report/RESULT_STATUS, the one read/search
  rule with per-harness tool names and the legitimate fallback, permission-prompt behaviour); tp-spawn injects it on every route
  (spawn, retry, review, lane, contract, bench) independent of the manifest; manifest stays optional for project tools; reviewer
  gets its role text (N1); one read/search rule reconciled across roles, prompt-examples, tool descriptions (`safe.ts`, `safe.mjs`),
  `tp-review`, retry notes; `launch.json` per task (HB0); Main's own launch record from tp-main; Agent-tool children: the contract
  goes in the Agent tool guidance Main and workers carry, and unsupported routes are listed. Provider adapters state mechanics and a
  `capabilities` list; unsupported shows in launch.json.
- **Depends on:** SG1 merge (same files: coder.md, tp-spawn). Merges/absorbs **H11** (reviewer search guidance), **H27** (git diff /
  globs into prompts), the instruction half of **H10**, SG1's tp-spawn block.
- **Benefit:** O1/O2 fall; bench and production get the same instructions.
- **Evaluation:** deterministic: a delivery table test renders the effective package for every route x harness and checks the
  contract and the right tool names are present (fail-before on main); real dispatch/retry/review on battle-soccer scratch clone
  with Claude and pi; count shell grep/sed on candidate vs baseline and permission prompts per task (HB8 E1).
- **Evidence:** `hub/hb2/`. **Decision:** pending.

### HB3 durable memory and Main's working brief (H36)
- **Problem:** finding 3, 4, O4, O5; 52 compactions with no checkpoint. **Hypothesis:** writing decisions when they happen into the
  existing work store, and rebuilding a short brief from it at every start/compaction, keeps corrections in force.
- **Scope:** reuse `tp-agent work` (workspace, decide, checkpoint, recover; `docs/persistent-work.md`): auto-attach a workspace per
  goal; decision record gains `kind` (preference | design | task-intent | obligation), `scope`, `provenance` (owner quote + time, or
  `inferred` = provisional), `status`, `supersedes` (borrowing product-design's supersession and fingerprint, not its package);
  `tp-agent brief` builds the working brief (outcome, acceptance, active preferences in scope, active work, open decisions, pending
  checks from HB5, pointers); delivered by SessionStart/precompact hooks (Claude) and the pi equivalent; decision-recording lines
  into Main's prompt; dispatch pulls in-scope preferences into the worker packet; lane cleanup surfaces lane-local knowledge.
- **Depends on:** HB0. Absorbs the record side of **H19** (criterion versions) and **H25** (an ack must not replace a result).
- **Benefit:** a correction stays effective; return without reconstruction. **Evaluation:** HB8 E4 (compaction/restart) and E5
  (recurrence) with negative cases (out-of-scope preference must not apply). **Evidence:** `hub/hb3/`. **Decision:** pending.

### HB4 worker correction operation (H37)
- **Problem:** 5, 6a, 6b, O3. **Hypothesis:** a state-aware `tp-agent correct` with stable request ids makes routine corrections
  reliable without the owner's terminal procedure.
- **Scope:** read state (state.json, pane, request record) → classify (input, permission, busy tool, queued text, completed,
  unknown) → cancel where supported (pi: RPC `abort`/`clear_queue`/`steer`; Claude: Escape, then verify the prompt is gone; a
  permission prompt is **cancelled = denied**, never approved) → verify ready → deliver with the MS2 delivery id → confirm receipt
  → report resumed or not. States: requested, confirmed, failed, unverified. A dependency hold stays when interruption is
  uncertain. Request ids (`req-…`) from hooks for every permission/input/capability request; notifications keyed by request id;
  resolving a request is separate from delivering a message; bounded retries with an idempotency key per correction; queued text is
  saved before clearing. Pause recorded only after a confirmed interrupt; the Claude Main gets the watchdog it lacks.
- **Depends on:** HB0 (request id, event line). Absorbs **MS1/MS2** follow-ups, **MN1** identity, **H24**, **H25**, the
  stall-detection trigger of **H9**.
- **Benefit:** no messages into dialogs, no duplicates, no false "paused". **Evaluation:** deterministic state-machine tests with
  fake panes in private tmux servers; installed-version runs with real Claude 2.1.322 and pi 1.1.0 workers in each state (HB8 E2,
  E7). **Evidence:** `hub/hb4/`. **Decision:** pending.

### HB5 applicable verification as obligations (H38)
- **Problem:** O6; reminders to run eval tools (O5). **Hypothesis:** per-task obligations from a check catalogue, reusing valid
  results, make applicable checks happen without reminders and cut duplicates.
- **Scope:** check catalogue (extends the tools manifest): applicability (paths, languages, change kind), purpose, required |
  advisory, expected evidence, cost; project checks (fallow, jscpd, depcruise, ast-grep, fast-check, project tests) vs harness
  evals (prompt/routing/memory/worker-control changes → HB8 episodes). Obligations pending | satisfied | inapplicable | deferred
  with reason; surfaced in the brief and before completion/lane close; advisory findings listed, never auto-applied. Reuse through
  **H33 VR1** `tp-check` (freshness key) — VR1 becomes this item's evidence layer. Stage hooks from the code-quality eval ("Where the
  checks run") are the delivery mechanism.
- **Depends on:** HB0, HB3 (brief shows obligations), VR1. Absorbs **H33 VR1**, **H8**, **H28**, lane close evidence (B2), and runs
  **H29-H31/FC1** tools only as catalogue entries for target projects. **Evaluation:** HB8 E3 incl. negative cases (irrelevant check
  must not run); VR1's invalid-reuse = 0. **Evidence:** `hub/hb5/`. **Decision:** pending.

### HB6 waiting and supervision visibility (H39)
- **Scope:** **H32 WT1** is this item; adds dependency relations (task → task/lane/review/owner) and normalised tool names (fix the
  readers in finding 8); unknown stays unknown; elapsed vs accumulated never mixed; a status view: objective, progress, blocker,
  next valid action, required decision (fed by HB3's brief). Absorbs **H9** measurement and decides **H16**. Depends on HB0 event
  journal and HB4 request ids. **Evaluation/decision:** WT1's thresholds. **Evidence:** `hub/hb6/` and WT1's plan.

### HB7 recurring corrections to scoped improvements (H40)
- **Scope:** a friction record (trigger, correction, successful action, scope, evidence) written from HB3 decisions of kind
  `correction`; Main proposes the durable home (contract line, tool default, adapter, retrieved procedure); owner-stated = active in
  scope, inferred = candidate; worker text never becomes policy. A prune pass: each contract/role line carries the evidence that
  justified it, and removal is evaluated like addition. Absorbs brief-rule signals **H12, H22, H26** as their first records, and
  **H15** (model-specific profiles) as the "simpler workflows for newer models" arm. Depends on HB3, HB8. **Evidence:** `hub/hb7/`.

### HB8 complete-episode evaluations through Main (H41), alongside HB2-HB4
- **Scope:** an episode runner that starts Main (tp-main, Claude) on a battle-soccer scratch clone in a private tmux server
  (TMUX_TMPDIR + `-L`), gives it a goal, plays scripted owner turns (one process correction allowed), forces compaction/restart
  where the episode says, and scores from records. Keeps tp-bench worker tasks as they are. Episodes E1-E7 = the owner's table,
  each with a negative variant. Deterministic checks (delivery, state transitions, request identity, evidence freshness) run every
  time; live runs repeated (3 per arm), then held-out variations; human assessment for semantic success. Measures: the owner's list
  (correct work, process corrections per episode, recurrence, recovery success/time, avoidable waiting, false completion claims,
  irrelevant checks, stale/redundant verification), product decisions counted apart from process reminders. Baseline = main before
  HB2; candidate = each merged item.
- **Depends on:** HB0 for scoring fields; baseline runs need nothing. Absorbs the Main-level part of **J1/tp-bench**.
  **Evidence:** `hub/hb8/`. **Decision:** pending.

### HB9 work-Mac handoff (H42)
Exact commit, runtime versions, baseline vs candidate results incl. unresolved failures, portable config changes and migration
steps, executable smoke checks (dispatch, safe-tool guidance, correction, memory recovery, evidence reporting), known limits, a
short validation checklist. Machine-specific settings and credentials stay out. Simulated work-only integrations (GL1) are labelled
simulated. **Evidence:** `hub/handoff/`.

## Reconciliation with active and queued work
- **SG1 (lane safe-search, round 10 running):** kept as is; merges first. HB2 starts from it and fixes its tool-name mismatch.
- **H32 WT1 → HB6; H33 VR1 → HB5 evidence layer; H9 → HB4 trigger + HB6 measure; H16 decided by HB6; H11, H27 → HB2; H24, H25 →
  HB4/HB3; H19 record side → HB3; H8, H28 → HB5; H12, H22, H26, H15 → HB7; J1 Main-level → HB8.** Their rows stay for history with a
  pointer.
- **Code-quality eval, H29-H31, FC1:** unchanged as target-project evaluations; their adoption plugs into HB5's catalogue.
- **H34 GL1:** stays deferred; its fixture is the simulated GitLab contract for HB9.
- Not merged: H1-H7, H13, H17, H18, H20, H21 (separate goals).

## Sequencing and lanes
1. Now: HB0 `interfaces.md` (Main). HB8 baseline episodes need only main and can start with the harness.
2. Lanes (each `--base main`, rule-first briefs, attack/fail-before tables, realistic-use bar, cross-provider review):
   `hub-correct` (HB4) and `hub-episodes` (HB8) start first, since neither touches SG1's files; `hub-memory` (HB3) next;
   `hub-delivery` (HB2) after SG1 merges. HB5, HB6, HB7 after HB2-HB4 land.
3. Each item ends with an adopt/revise/defer line here, backed by HB8 results.

## Independent assessment (2026-11-10)
The Fable assessment is mapped in `../assessment/README.md`. It confirms the HUB order. Its additions went into HB2, HB4b, HB5, HB6 and HB9 as notes on H35, H37, H38, H39 and H42; its new items are H43-H55. H48 (labelling one campaign's interventions) should run before HB7 is scoped.

**Target Main (owner, 2026-11-10):** "lets only worry about claude main for now and bring pi main up once we have the processes all
working". HUB items are built and judged on Claude Code Main. Pi-only parts (the pi watchdog in row 6a, pi handler parity) are
follow-ups after the processes work, not blockers.
