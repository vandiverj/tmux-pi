# C: durable memory, verification evidence, observed failures (read-only audit, main 6c8094f, 2026-11-10)

Classes: **OBS** = observed failure (evidence path) · **SRC** = source-confirmed gap · **HYP** = hypothesis.
`TASKS` = `.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks`. Main transcript = `~/.claude/projects/-Users-jerrickvandiver-Documents-Projects-tmux-pi/6061f696-...jsonl` (98.6 MB, 42,685 lines).

## GOAL 1a: memory / decision / checkpoint stores

| Store | Path | Schema | Writer | Reader | Read by Main at start / restart / compaction? | Scope / provenance / supersession |
|---|---|---|---|---|---|---|
| Work store: workspaces + notes | `~/.config/tmux-pi/work/{workspaces,notes,archive}` (lib/work.py:5, docs/persistent-work.md:12) | MD + frontmatter; `FRONT_ORDER` id,title,status,updated,repos,tickets,tags,goals,workspaces,important,sources,superseded_by (lib/work.py:34); sections Objective, Scope, Decisions, Open decisions, Blockers, Next actions, Links, Progress, Sessions (:27) | `tp-agent work new/decide/decision add/next/...` (lib/work_commands.py) | `work recover`, `tp-agent find`, task index (`work` key) | Designed yes: SessionStart(compact) injects index (hooks/sessionstart-compact.sh:40); prompt says run `work recover` (agent/orchestrator-claude.md:12). **Only when a workspace is attached.** | Decision fields Question/Background/Options/Recommended/Affects/Resolution/By/At/Quote/Withdrawn (work.py:28); status open/answered/withdrawn; `--by user --quote` provenance (persistent-work.md:83); notes have `superseded`, `superseded_by` |
| Work checkpoints | `<store>/checkpoints/<ws-id>/*.json` (persistent-work.md:36) | sha256-validated judgement copy, `carried_from`, `truncated` | PreCompact hook (hooks/precompact.sh:11-12), any `work` mutation, `tp-exit`, manual | `work recover` | Yes, if attached | agent summary labelled agent-authored |
| Recovery marker | `<state>/session-recovery/`, `session-recovery.json` | generations | precompact + sessionstart-compact hooks, `work attach`, `autonomy on` | autonomy gate | Gates dispatch only with `autonomy on` | — |
| Goal records (Pi only) | `<project>/.tmux-pi/goals/` (docs/goal-mode.md:3) | spec id/objective/done/scope/allowed_paths/budget | `bin/tp-goal` → lib/goal.py | Pi dispatcher.ts | Session reload restores attachment only (goal-mode.md:17) | approve-sha256, human confirm tokens |
| Run ledger | `<state>/ledger.jsonl` (bin/tp-ledger:11) | task, worker, model, elapsed, tokens, artifact, stop, rating | `tp-ledger record/rate` (research tasks) | `tp-ledger` | No | rating is human |
| Knowledge store (CX1) | `<project>/.tmux-pi/knowledge/{approved,candidates,superseded}` (docs/context-engine.md:11-15) | strict frontmatter: id, kind (convention/framework/relation/decision/lesson), scope globs, tags, versions, source (human/review:/doc:/work:/worker:), evidence + evidence_hashes, supersedes (:27-39) | `tp-ctx knowledge add/import`, `ctx_note` (candidates); `approve` human-only TTY (:111) | `ctx_brief`, `ctx_knowledge` for workers with `TP_CTX_TOOLS=1` (:121-124) | **Main never** ("main itself does not", :124) | Best provenance model in repo: scope globs, evidence hashes → `stale_by_path`, `supersedes` |
| Product-design package | `DIR/{package,design,reviews}.json` + `DIR/.tp-product/{commit.json,history/NNNNNN.json}` (docs/product-design.md:102-108, 288-296) | ids `<kind>.<slug>`; claims/rules/decisions/questions/slices/cases; x-ref/x-dep links (lib/product_design/schema.py:9-13) | `tp-product` (lib/product_design/cli.py) | same; review page | No (opt-in workflow) | rules/decisions `superseded_by`/`supersedes`; decision `acceptance` {who, when, evidence, fingerprint, origin:local}; fingerprints over x-dep closure; slice readiness draft→ready-for-human-review→ready-for-case-preparation→implemented-verified with `delivery` evidence (product-design.md:146-228) |
| Session task index | `<state>/session-index.json` | schema, updated, objective, active, pending, unstatused, paused, recent, lanes | `tp-agent index` | injected at SessionStart / compaction | Yes (the one thing Main actually gets) | none; `objective` empty in current session |

**What is actually populated (OBS):**
- Work store is **empty**: `~/.config/tmux-pi/work/workspaces`, `notes`, `archive/*` are empty dirs (created 2026-11-07 19:31); no `checkpoints/`. `session-index.json` has `objective: ""` and no `work` key. So the whole checkpoint/recover/decision machinery is inert for this campaign.
- No `.tmux-pi/knowledge/`, no `.tmux-pi/goals/`, no `ledger.jsonl`, no `.tp-product` package in Projects/*.
- Main's transcript holds **52 compaction summaries** (`"isCompactSummary":true`, 2026-11-08T06:32Z → 2026-11-11T04:07Z). Main transcript lines mentioning `tp-agent work `: 7; `work checkpoint`: 0; `work recover`: 1 (likely prompt text, not a run).
- De-facto durable memory Main relies on instead: Claude auto-memory (`.../memory/MEMORY.md` + 18 files), `docs/plans/det-prep/README.md` (157 lines), `run-log.md` (1029 lines, holds owner decisions e.g. :85 "owner decisions applied"), `loop/ideas.md`, 127 files in `docs/plans/det-prep/fixes/` (round briefs and Main "rulings" that workers are pointed to by message), `docs/plans/realistic-use-bar.md` (owner rule), and per-task `main-evidence/evidence.md`. None of these has id/status/supersession fields; owner decisions are prose in run-log and briefs.

**SRC gaps:**
- Work store only reaches Main when a workspace is attached; nothing auto-attaches or prompts to create one (persistent-work.md:26, :97-105). Compaction instruction is conditional ("If a workspace is attached").
- Knowledge notes are worker-only; Main gets none, and a worker in a worktree writes candidates into that worktree's `.tmux-pi` (context-engine.md:125).
- No store links an owner decision → the lane/brief/task it changed. `Affects` (`workspace|lane:<n>|task:<id>`) exists in work decisions (persistent-work.md:82) but is unused.

**Current working brief for Main could be assembled from (already exists):** session-index (active/pending/lanes) + attached workspace (Objective, Decisions, Open decisions with `Affects`, Blockers, Next actions) + newest checkpoint's agent summary + approved knowledge notes scoped to lanes' paths + lane.json steps/closed + last `main-evidence/evidence.md` per open lane. Missing piece: an owner-rule list (e.g. realistic-use bar, "never search config/ recursively", "workers use safe_grep") with supersession — today these live in MEMORY.md and run-log prose.

**Product-design traceability reusable for ordinary fixes:** claim(source-cited) → rule/decision (x-ref, accepted with fingerprint+evidence) → slice (x-dep closure fingerprint, readiness, `delivery` evidence: commit/merge-request/ci-run) → case. Stale-on-change via fingerprint and "model review can reach only ready-for-human-review" are directly reusable for "owner decision → brief → task → verification" chains. Cost: it is a heavy package format (3 JSON files + commit journal); a light reuse would borrow the id scheme, `supersedes/superseded_by`, acceptance-with-fingerprint and the readiness ladder only.

## GOAL 1b: verification evidence

| Record | Where | Carries | Missing |
|---|---|---|---|
| `tp-gate:v1` | `gate.json` beside log and `$TP_TASK_DIR/gate.json` (docs/agent-tools.md:56); fields lib/gate_record.py:48-51 | worktree, base{ref,sha}, head, tree, files, checks[{name,status,exit,seconds}], started, finished, log | per-check cmd/args, tool versions, config hash, env |
| check statuses | lib/gate_record.py:60 | `passed, failed, missing, not_applicable` | no `pending`, `deferred`, `waived` |
| required vs advisory | `--require NAME` makes a `missing` check fail (pre-commit-gate.sh:28-29, 659-663; gate_record.missing_required :187) | per-invocation only | no persistent per-project required/advisory policy; no reason field for not_applicable |
| `tp-test-run:v1` | `$TP_TASK_DIR/test-runs/<n>.json` (test-run.sh:111-136) | label, cmd[], cwd, head, tree, exit, timed_out, seconds, log, summary | start/end, versions, env; log in `$TMPDIR/tp-tools` |
| `tests.jsonl` | tests/run_bounded.py | cmd, files, seconds, rc, load | tree |
| freshness | `gate_record.status()` fresh/stale/invalid (:171-180) | head+tree equality | **no production caller**: `git grep gate_record` in lib/, bin/tp-agent, bin/tp-review, bin/tp-spawn, tools/ hits only comments (tools/agent/_common.sh:105, json_record.py:8, agent_tools.py:45) — SRC |
| lane close | lib/lane_steps.py:299-387 | worker finished, review whose `reviewed-state.tree` == current tree (:336-341), approved, committed | **no gate/test evidence**: B2 (`lanes.require_evidence`, docs/plans/det-prep/b2-lane-close-evidence.md:7-12) not implemented (`git grep require_evidence` hits only lib/ctxengine/store.py) — SRC |
| `tp-review --evidence FILE` | bin/tp-review:45, 254-255, 390-393 | copies file under "Evidence supplied by Main", tells reviewer it is unchecked | no provenance/freshness check of the file |
| acceptance items | bin/tp-review:67, 307 → `acceptance-items.json` | per-item met / not met / not checked + evidence | closest existing "satisfied/pending" ledger; per review only |
| tools manifest | lib/agent_tools.py, `<workdir>/.tmux-pi/agent-tools.json` | name, path, roles, use, usage, output | no applicability/required flag; **tmux-pi itself has no manifest**, so the STANDING lines (incl. "Read a range with ... safe_read, not sed -n", agent_tools.py:43) never reach tmux-pi workers (docs/agent-tools.md:123 "No manifest: nothing changes") — SRC |

Usage in this session (OBS): only 9 of 416 task dirs have `test-runs/`; **0** have `gate.json`; 56 have `tests.jsonl`; 121 have free-form `main-evidence/`. Duplicate verification is visible, e.g. `TASKS/coder-2bde9a72ff/evidence/cell-{brew,system}-{inside,outside}.log` (worker's 4 cells) and `main-evidence/sg1r9-py{39,314}-{in,out}.log` (Main's same 4 cells).

**Reconciliation with existing plans (do not duplicate):**
- **VR1** (docs/plans/det-prep/vr1-verification-reuse.md) already inventories gate/test-run fields, the unused `status()`, the two tree algorithms (review_rounds.snapshot vs gate fingerprint, :19-21), no in-run change detection, no dedupe, untrusted `--evidence`, and proposes an equivalence key + `tp-check` reuse/join/fresh and lane close requiring a fresh record (:48). Not covered by VR1: required/advisory policy per project, `pending/deferred/inapplicable-with-reason` statuses, linking a check to the requirement/decision it verifies (VR1 defers criteria to H19), and the empty-manifest problem.
- **WT1** (wt1-wait-timeline.md) covers timing: events.jsonl, queue reasons, lease log, test-run start/end (:34), permission-wait start/end (:35). Its item 5 overlaps the permission findings below; WT1 measures, it does not fix redirect loops.
- **B2** (lane close evidence) is the planned but unmerged home for "close requires evidence".

## GOAL 2: observed failures in real transcripts

Sample: 416 task dirs; 72 have `messages.jsonl` (104 Main messages); 30 have `permission-request.json`. Coder transcripts sampled: SG1 r1/r5/r9/r10 (coder-655a0e0bf5, 14a568610a, 2bde9a72ff, ea8e9fabc5), PD-E (ffa36eecbe), PD-C (ea5ce10750), MS2 (a74d102fff), G3 (cca7c4bbae), SW1 (6f13829428).

### F1 Workers use shell grep/sed/cat instead of safe tools — OBS
Bash calls whose first command is grep/sed/cat (per transcript, via `transcript-path`): SG1 r1 coder-655a0e0bf5 13/9/9 (+15 `safe_grep`); SG1 r5 coder-14a568610a 21/21/3; SG1 r9 coder-2bde9a72ff 17/22/5; PD-E coder-ffa36eecbe 30/16/13; MS2 coder-a74d102fff 20/27/13; SW1 coder-6f13829428 24/22/7; PD-C coder-ea5ce10750 21/12/5; G3 coder-cca7c4bbae 11/4/1. Only the first SG1 round used `safe_grep` at all. `packet.md`/`task.md` of all sampled tasks contain 0 mentions of `safe_grep` (incl. those after the owner's 14:55Z complaint). Cause SRC: no `.tmux-pi/agent-tools.json` in tmux-pi → STANDING line absent; no `safe_grep` in agent/roles/coder.md, code-reviewer.md, orchestrator*.md, lib/preamble.py (grep -c = 0). Fix is in flight as lane SG1 (round 10 brief e4a478f), not merged.

### F2 Permission prompts and Main redirects — OBS (≈30 tasks)
`permission-request.json` targets: recursive search of config/agent/claude/tools (12: e.g. `TASKS/review-coder-40e8a7a43a-153555`, `review-coder-b812bd22f1-155918`, `review-coder-1f75f603ab-091841`, `review-coder-a74d102fff-230635`); `.env` text in command (6: e.g. `review-coder-b8fd822d83-100331`, `review-coder-c13cfc9e4c-235943`); unscoped `git diff` (5: `coder-7ca71a3733`, `coder-f2f7dc5d9d`, `coder-653e1a5433`, `review-coder-5633e87e87-045109`, `review-coder-9f56acfbce-075533`); named sensitive file (`claude/mcp.json` in review-coder-5b6d0da0af-090604, `config/local.json` in review-coder-655a0e0bf5-104043, `bin/tp-secret-exec`). Main's redirect texts repeat the same rule ~30 times ("do not search agent/ or config/ recursively", "any command whose text contains the string .env ... raises the prompt"). Repeat offences in one task: review-coder-b8fd822d83 (2), review-coder-603eb62dce (2), review-coder-a74d102fff (2), coder-653e1a5433 and coder-4d766446d8 ("second permission prompt ... task is parked").
- SRC: rules are relayed per-incident by message; packets do not carry them up front (F1 cause).
- OBS workaround drift: 4 reviewer messages tell workers to append `|| echo exit-code: nonzero` because "auto mode refuses a command the redaction hook changes" (review-coder-0fef98c95d-073102, -4d766446d8-074323, -7ca71a3733-073229, -f6352fbb8b-085811).

### F3 Worker stuck at a prompt when a message was sent — OBS
- `TASKS/coder-62fe70c08d` (U1): Main message 2026-11-08T06:35:29 ("I denied that command..."); `state.json` still `"state":"permission"`, `"tool":"Claude needs your permission"` at 06:35:38; `stopped.json` 06:36:07; **no result.md**.
- `TASKS/review-coder-76b4f649f5-135407` (F1 strong-shown): permission denied 00:26:29, no message logged, `stopped.json`, `result.md` is an interim sentence ("I'm checking ... then running the affected routing ... tests").
- Stale records: `coder-cca7c4bbae/permission-request.json` and `coder-65193187aa/permission-request.json` still `status: pending` after Main declined (cca7c4bbae message 10:48:39). `permission-request.json` keeps only the last request (b8fd822d83 had 2 declines, file shows 1) — SRC (WT1 item 5 covers timing only).
- HYP: `review-coder-07bbf506d8-210919` was denied (03:10:12) with no `messages.jsonl`; the redirect either went unlogged or was not sent.

### F4 Lost / duplicated messages and replies — OBS
- Lost task packets: six coders (014ac74821, 40e8a7a43a, 6c8b009b46, 983705af23, d6ff23c1d0, dbcf0c11b8) got two re-deliveries, 16:38:06 "a startup dialog swallowed it" and 17:13:08 "login had expired" — the first resend also failed.
- Owner, 2026-11-09T21:20:28Z: "messages to workers not getting sent is one of them" (→ MS1/MS2). Messages before 2026-11-10T03:34Z have no `submitted`/`attempts` fields, so delivery of 78 earlier messages is unverifiable (SRC, fixed forward by MS2: 26 later messages all `submitted: true, attempts: 1`).
- Reply clobbering: result.md overwritten by an ack, Main asks for a re-send: coder-256b4dd5bc ("Your one-line ack replaced result.md"), coder-4a28b45a70, coder-61108a8b31. RO1 (result-kept, coder-c28747a7ad laterReplies) targets it — HYP that it is fixed.
- Missing `RESULT_STATUS` line forcing a re-reply: coder-52ad5a71e4, coder-86b4ed5fe2, coder-be089069b2 ("(not 'done')"), coder-6f13829428, coder-d3bea0b5b2, coder-0644087939, coder-8368348c70.

### F5 Main corrections / scope changes mid-task — OBS
Brief errors or late additions: coder-7ca71a3733 ("The brief's 'TP_BENCH_*' label was wrong"), coder-0fef98c95d (ownership grant then "do not edit tests/test_tp_claude_suffix.py ..." 6 min later), coder-36ec1ecbe7 ("one more change in this round, before review"), coder-3a0d4cbc7c, coder-51f93f6064, coder-eba72607ca ("required addendum to your round 8 brief"), coder-ea5ce10750 (owner bar replaced round-7 attack scope). Lane-ownership grants by message (8 tasks: 0fef98c95d, 7ca71a3733, f2f7dc5d9d, 86b4ed5fe2, 81dc9a8dd3, c28747a7ad, dc15a6aff4, f6352fbb8b). Decisions travel as message + `fixes/*.md` file, never into a decision store.

### F6 Owner process reminders in Main's transcript (62 owner-typed lines after filtering notifications)
Raw `grep -c` on all `"type":"user"` lines without tool_result: safe_grep 8, safe_read 3, fallow 5, Escape 52, remind 52, "permission prompt" 54, "you forgot" 0 — mostly compaction summaries/notifications, so not owner signals. Owner lines that are process corrections:
1. 2026-11-09T17:23:21Z "why do yo think lm studio is not available? you should be able to send it a small test prompt to verify"
2. 2026-11-09T21:20:28Z "messages to workers not getting sent is one of them"
3. 2026-11-09T22:02:29Z "my main is claude code, side tasks are subagents"
4. 2026-11-10T00:50:32Z "...almost as though it found one or two things and quite rather than checking its own work..."
5. 2026-11-10T13:27:25Z "explain to me what you are trying to do with the summarizer because I am not planning to have it used by outside systems"
6. 2026-11-10T14:55:53Z "workers do not use safe_grep/read from the beginning and that main does not tell them to ... a lot of workers are trying - grep -C until I told main to have them use safe commands"
7. 2026-11-11T03:19:14Z "shouldn't you be evaling it against battle soccer being used by tmux-pi"
8. 2026-11-11T03:20:31Z "these tools are not for tmux-pi to check itself but for tmux-pi to check the source code that is being worked on agentically"
Each is a durable rule that today lives only in MEMORY.md (lm-studio-always-up, workers-attack-own-rule, cq-tools-target-projects, realistic-use-bar) and not in any store Main's workers see.

## Classification summary
- OBS: work store empty / 52 compactions with no checkpoint; 0 gate.json, 9 test-run dirs; duplicate worker+Main cell runs; F1-F5 above.
- SRC: no production freshness caller; lane close ignores evidence (B2 unmerged); `--evidence` unchecked; tmux-pi lacks agent-tools manifest so safe_read standing line never ships; packets/roles never name safe_grep; permission-request keeps last only and goes stale; pre-MS2 messages lack delivery fields; knowledge store never reaches Main; decisions not linked to tasks.
- HYP: 07bbf506d8 redirect unlogged; RO1 fixed result clobbering; SG1 merge will fix F1 only if the packet/role text changes too (round briefs are about the guard, not the brief template).
