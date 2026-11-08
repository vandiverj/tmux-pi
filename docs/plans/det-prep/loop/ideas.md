# Candidate hypotheses

Status: open | running | kept | discarded | refuted (by data, no run). Fingerprint = files + mechanism in one line.

| id | idea | metric it should move | source | cost | status |
|---|---|---|---|---|---|
| H1 | **Local doc lookup for coders.** Index framework manuals and the project's own docs (React, Vite, vitest, TS handbook pages actually used; `docs/agent-tools.md`; worker rules) into a local search the worker calls as a tool; the local model returns the 3 to 8 most relevant lines with `path:line` refs, never a summary, so the frontier worker reads only those ranges and can open more by ref. | code_pass, tokens_per_accepted, orient_waste | owner | medium | open |
| H2 | **Local scout before dispatch.** Before a coder starts, the local model runs the brief's searches (symbols named in the brief, callers, tests that name the files) and attaches a `## Where to look` block of `path:line` refs with one-line reasons; the coder is told to start there. Measures whether pre-located refs replace frontier grep/read loops. | orient_waste, tokens_per_accepted | owner | low | open |
| H3 | **Local model as the search tool.** A worker tool `ask_local "question" PATHS` that greps and reads with the local model and returns refs plus quoted lines, used instead of repeated `sed -n`/grep calls in frontier context. | tokens_per_accepted | owner | medium | open |
| H4 | **Tool usage manual injected into the local model.** Give the local model a short manual for tmux-pi tools and git/grep forms (the safe forms from the worker rules) and test whether its run_named_checks and locate_quote accuracy rises. | local_exact | owner | low | open |
| H5 | **Syntax cards for the local coder.** For `small_edit` local tasks, inject a compact card of the exact APIs the file imports (signatures pulled by the ctx engine from node_modules types) and measure local small-edit pass rate. | code_pass (local subset) | owner | medium | open |
| H6 | **Affected-areas block sourced by the local model** instead of grep only (D2), for languages the navigator cannot read. | review_catch | Main | medium | open |
| H7 | **Blind second reviewer** (no author result at all) on steps whose first review approved, local model or small level, only to raise disagreements. | review_catch, review_fp | Main (earlier research) | medium | open |
| H8 | **Report-template autofill**: the runner fills counts, log paths and commit from B1 records into the worker's report so workers stop spending turns on it. | tokens_per_accepted | Main | low | open |

| H9 | **Start check.** tp-spawn (or the status poller) confirms a worker actually took its first prompt (transcript has a user turn, or the context counter rises) within N minutes, and flags the task `stalled` with the pane's last lines (for example "Please run /login") instead of reporting "working". Found live: 6 coders idle 80 min behind an expired login. | wall_s, deterministic failure detection | Main (observed 2026-11-07) | low | open |

| H10 | **Haiku and the redaction hook.** With redaction on, lib/redact_hook.py rewrites every Bash command (appends `|| echo exit-code: nonzero`); Claude Haiku workers treat the rewrite notice as untrusted and stop running Bash, so their steps return incomplete. Fix options: do not rewrite (use PostToolUse only), word the notice so it reads as the harness, or route Claude coders above Haiku when redaction is on. Found live 2026-11-07 (E1, G1). | code_pass, wall_s | Main (observed) | low | open |

| H11 | **Reviewers stop on the sensitive-read prompt.** Four of eight reviewers so far asked for a recursive search of agent/, config/ or tools/agent and waited on "Authorize sensitive read?" until Main denied it. Tell reviewers in the packet which paths are secret stores and to search named files or subfolders, or make the guard exclude known secret files from the search instead of asking. | wall_s, Main turns | Main (observed 2026-11-07) | low | open |

| H13 | **Structural sweep with ast-grep.** When a review finding names a family, Main or the coder writes an ast-grep rule (pattern with metavariables, or YAML rule) for the defect shape; `ast-grep scan` lists every site in `bin/`, `lib/`, `agent/extensions/`, `tests/` before the fix, the same rule runs after the fix and must return zero (or only allowlisted) hits, and the rule file is kept as the parity check. Targets the F1/D1/K1 pattern of 5–8 rounds where each round fixed only the cited site. | rounds_to_approve, review_catch, tokens_per_accepted | owner (2026-11-08) | medium | open |
| H14 | **RTK command-output compression** (`rtk-eval` branch; eval done 2026-11-08: all 4 decision items fail; results on branch rtk-eval `docs/plans/rtk-eval/results.md`, f846a00). | tokens_per_accepted at equal code_pass | owner (2026-11-08) | low | rejected |
| H15 | **Model-specific worker profiles** (guided vs expert instructions, planning scaffold, tool surface per model group; `m1-model-profiles.md`, arXiv:2609.20804). | code_pass, rounds_to_approve, tokens_per_accepted | owner (2026-11-08) | medium | open |
| H16 | **OpenTelemetry usage monitoring** (Claude Code built-in OTel; Pi matched by an extension emitting the same event shape). Per-request tokens/cost, per-tool result sizes and durations, permission decisions, API errors, joined to tmux-pi task ids. Eval measures what it reveals and what it costs before any adoption. | orient_waste, tokens_per_accepted (attribution), wall_s overhead | owner (2026-11-08) | medium | open |
| H17 | **Strong-model traces → smaller-model harness aids (X1).** Mine verified Sonnet/Opus traces on Battle Soccer for packet facts, tool/schema fixes, deterministic helpers, hooks and short skills; A/B/C/D ablation on the small model with a sealed holdout. Plan: `../x1-trace-distillation.md`. | accepted completion w/o escalation, first-call validity, tokens/cost per accepted | owner (2026-11-08) | high | queued |
| H18 | **pi-hashline-readmap for the Pi coder** (hash-anchored read/edit, structural map/symbol reads, ast_search), plus which ideas port to Claude Code (MCP tool or existing ctx tools). Plan: `../hl1-hashline-readmap.md`. Guard parity and Pi 1.1.0 compat are gating. | edit first-try success, silent wrong edits, output tokens per edit, tokens_per_accepted | owner (2026-11-08) | medium | queued |
| H19 | **Requirement-change handling.** Mid-case, an acceptance criterion changes; the harness must find and invalidate the work, evidence and reviews that depended on the old criterion, and redo only those. | stale-evidence accepted (must be 0), rework scope precision, rounds | owner (2026-11-08, "things to think about") | high | future |
| H20 | **Semantic integration conflicts.** Two lanes each pass on their own but disagree about a shared API or business rule; the merge is textually clean. Does integration catch it before `lane/integrate` accepts both? | conflicts caught before merge, false alarms | owner (2026-11-08) | high | future |
| H21 | **External-state reconciliation.** An MR/deployment succeeds but the response is lost (crash, timeout); on resume the harness must detect the action already happened and not repeat it. | duplicate side effects (must be 0), correct resume state | owner (2026-11-08) | high | future |

Research runs append below this table with their ids.

## H12: fix briefs ask for the rule, not the sample (2026-11-07)
E1 and G1 each took 3 rounds in one finding family. Both coders patched only the site or form the reviewer cited: E1 kept extending a regex for git option forms, and G1 moved the unload past one kill step at a time. Idea: when a finding names a family, the fix brief must state the rule that closes it (a parser, or "the unload goes before anything that can end the caller"). It must also require a test that proves the rule, either a parity table or a stub that kills the caller, not one example. Eval: rounds-to-approve on review-loop tasks in tp-bench.

## H11 update (2026-11-07): the cause and two fixes
The cause is a gap in the instruction plus a conservative classifier.
- Instruction: `bin/tp-review:219` scopes safe_grep to files outside the worktree, so reviewers use bash `git grep` inside the worktree.
- Classifier: `lib/sensitive_read.py:61` (CREDENTIAL_DIRS) flags agent/, config/ and claude/ as search roots even for `git grep`, which can only read tracked files.

Fix A (instruction, bin/tp-review): search the repository with safe_grep, or use `git grep` with roots outside agent/, config/ and claude/. Read files in those directories by exact path.

Fix B (system, lib/sensitive_read.py): a plain `git grep` clears a credential-dir root when `git ls-files` under that root lists no credential-named file. Plain means no --untracked, --no-index, --recurse-submodules or revision. This is security code, so it gets its own lane and a security review.

Eval: reviews that stop on a prompt, per review. 8 of 17 before the fix.

## H13: ast-grep for whole-family sweeps (owner, 2026-11-08)
Owner: "this could possibly help with the issue of not finding all the places a defect happens and then we have to do 5 rounds of single file changes instead of one".
Evidence from det-prep:
- F1 took 8 rounds and D1 took 7. K1 and C2 took 3 each.
- Several rejections were a "new missed sweep site" in the same family. One example is F1 r7, where `tp-contract` called `write_record` without `relative=True`.
- Coders' sweep tables come from text grep. Text grep misses call forms, for example a call wrapped across lines or a keyword passed through `**kwargs`. It also floods results with comments and strings.

How ast-grep could close this:
- ast-grep (https://ast-grep.github.io/) matches tree-sitter syntax, not text. It has built-in Python, TypeScript/JavaScript, Bash, JSON and YAML, which covers everything tmux-pi has.
- `bin/tp-*` scripts have no extension. `sgconfig.yml` therefore needs `languageGlobs` to map them to python or bash by shebang group, and the eval must check this.
- Patterns use metavariables, for example `write_record($$$ARGS)` with a `not: has: relative=True` constraint. YAML rules support `inside`/`has`/`not` relations and `--rewrite` fixes. `ast-grep test` checks rules against valid/invalid snippets.

Eval design:
1. **Replay (no new runs).** For each det-prep finding family that needed more than one round (F1 retirement-dependency-order, D1 diff-scope states, K1 option allowlist / committed check, C2 heading parse, E1 git option forms, G1 unload order), write the rule a reviewer could have written from the round-1 finding. Run it at the round-1 commit.
   - Measure: did it list the sites later rounds found (recall), and how many false hits did it return (precision)? Compare with the text-grep sweep the coder actually produced.
   - Rule-writing cost: time and tokens per rule, written by Sonnet and separately by the local Qwen.
2. **Live A/B (tp-bench review-loop tasks).** Fix-brief arm A is the H12 rule-first brief, which is what we use today. Arm B is the H12 brief plus an ast-grep rule file that the reviewer or Main supplies. The coder must run it before and after, and attach both outputs. The parity test then runs the rule.
   - Metrics: rounds_to_approve, review_catch, tokens per accepted step.
3. **Where it fails.** Note families that ast-grep cannot express, such as ordering across processes, concurrency races and semantic invariants. Those still need H12 tests.

Install for the eval: one pinned release binary, checksum-verified, in an isolated dir, the same way as RTK. Don't use global installs without the owner.
Adopt if: replay recall beats the text-grep sweep on at least 4 of the 6 families, live rounds_to_approve drops, and tokens per accepted step don't rise.

## H16: OpenTelemetry usage monitoring (owner, 2026-11-08)
Owner: use https://code.claude.com/docs/en/monitoring-usage "to better understand what is occurring", standardize Pi to match for eval; do not adopt if having it on harms the system; eval effectiveness and cost first.

What we measure today: task records carry summed tokens per task (`lib/task_family.py` TOKEN_KEYS). We cannot see *where* inside a task the tokens went (which tool output, which retry, which permission stop).

What Claude Code exports (enabled by `CLAUDE_CODE_ENABLE_TELEMETRY=1` plus `OTEL_*` exporter vars, all per process env, so `bin/tp-claude` can set them per worker):
- Events: `api_request` (model, input/output/cache tokens, cost_usd, duration_ms, query_source), `tool_result` (tool_name, success, duration_ms, `tool_input_size_bytes`, `tool_result_size_bytes`), `tool_decision` (accept/reject and source: config, hook, user_abort...), `api_error`, `api_refusal`, `safety_event`, `user_prompt`. `prompt.id` links all events of one prompt; `agent_id`/`parent_agent_id` mark subagents.
- Metrics: `token.usage`, `cost.usage`, `active_time.total`, `lines_of_code.count`, `code_edit_tool.decision`.
- `OTEL_RESOURCE_ATTRIBUTES` can carry `tp.task_id`, `tp.role`, `tp.lane`, `tp.arm` so every event joins to our records.
- Content is redacted by default. Keep it that way: do not set `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`, `OTEL_LOG_TOOL_CONTENT` or `OTEL_LOG_RAW_API_BODIES`. `OTEL_LOG_TOOL_DETAILS=1` (Bash commands, file paths) is tested only as a separate arm because commands can carry secrets; it must pass through the redact rules before storage.

Questions it could answer (the "benefit" side):
1. Which tool outputs dominate input tokens (direct input for H14 RTK and the ctx work): top `tool_result_size_bytes` by tool and command class.
2. Where wall time goes: API latency vs tool duration vs waiting on a prompt.
3. Permission stops (H11): `tool_decision` with source `hook`/`user_reject` counts, instead of scraping panes.
4. Retries and API errors per task that today are invisible.
5. Reviewer vs coder vs Main token split, and subagent cost (`agent_id`).

Pi parity: Pi has no OTel exporter. Write a Pi extension (`agent/extensions/otel.ts`) that emits the same event names and fields from Pi's turn/tool hooks (api_request from usage, tool_result with sizes/durations) to the same receiver. Fields Pi cannot supply are left empty, never guessed. Qwen-local requests get cost 0 and model `qwen-local`.

Receiver: no install needed for the eval. A small stdlib Python OTLP http/json receiver bound to 127.0.0.1 that appends to a JSONL file under the session dir (`OTEL_EXPORTER_OTLP_PROTOCOL=http/json`). Do not use the `console` exporter: it writes into the worker pane and would corrupt the TUI and our pane scraping. Installing an OTel collector, Prometheus or Grafana needs owner approval.

Eval design:
1. **Overhead A/B (the "harm" side).** Same tp-bench tasks, telemetry off vs on (redacted), 3 repeats each on Claude Sonnet and on Pi/Qwen. Measure wall_s, CPU/RSS of the worker process, code_pass, tokens_per_accepted. Telemetry is client-side, so model tokens should be identical in distribution; any change is a red flag. Also test receiver-down: the worker must not stall or error if 127.0.0.1 is unreachable.
2. **Fidelity.** Telemetry token totals per task must match the task-record totals within 2%; Pi extension totals must match Pi's own usage.
3. **Insight value.** Run one det-prep-style lane with telemetry on, produce the five answers above, and list concrete changes they point to (e.g. a tool whose output should be truncated). Value = number of actionable findings not visible from current records.
4. **Storage/privacy.** JSONL size per task; grep the stored events for secrets with the redact rules (must be zero hits).

Adopt (default-on for evals, and optionally for normal runs) if: no measurable overhead (wall_s within noise, no stalls with receiver down), fidelity within 2%, zero secret hits, and at least 2 actionable findings. Otherwise keep it as an opt-in `TP_OTEL=1` for evals only, or drop it.

## H19–H21: future robustness experiments (owner, 2026-11-08, "somethings to think about")
These build on J1. Each becomes a new bench task kind, a reviewed bench change of its own per PROTOCOL "Adding an eval", once `tp-bench` exists. They are scenario tests of the harness, not of one model.
- **H19 requirement change.** Scenario: a lane step is mid-flight, or already reviewed, when the brief's acceptance criterion changes (for example, a rule threshold in battle-soccer `src/game`).
  - What exists: B1 gate records bind test evidence to a tree fingerprint. Nothing binds evidence, reviews or lane steps to the version of the *criterion*.
  - Probable mechanism: record each step's brief hash with its evidence and review. A brief change marks every dependent record stale, and `tp-agent` lists exactly what to redo.
  - Score: no stale evidence or review accepted after the change; redo set equals the truly affected set (precision/recall); extra rounds.
- **H20 semantic integration conflict.** Scenario: lane A changes a shared helper's contract (units, sign or rounding of a score rule) while lane B adds a caller that assumes the old contract. Each passes its own tests, and the textual merge is clean.
  - What exists: lane-overlap warnings by declared paths (seen today on `bench`/`test-env`) and `lane/integrate` module runs after each merge. Nothing checks contract agreement across lanes.
  - Probable mechanism: run the union of affected tests of both lanes after every merge, plus a cross-lane review of shared symbols that both lanes touch or call (symbol-level overlap, possibly with H13 ast-grep).
  - Score: conflicts caught before acceptance, and false alarms.
- **H21 external-state reconciliation.** Scenario: a step creates an external effect (MR, deploy, or a stubbed tracker comment), and the effect succeeds. The harness crashes or times out before recording it, then resumes.
  - What exists: in campaigns, tmux-pi never merges to main or pushes, so test this against a local stub service that records calls.
  - Probable mechanism: an intent record written before the call, with an idempotency key. On resume, query the external state by that key before retrying.
  - Score: duplicate effects (must be 0), correct resume state, and time to reconcile.
- Order: after J1, M1 and X1 pilot. H20 is the cheapest to stage, since it needs only two lanes on battle-soccer. H21 needs the stub service.

## F-SESS (found 2026-11-08): explicit TP_SESSION loses to TMUX
- **Bug.** `tp_session` (`lib/tp.sh`) works out the session from `TMUX`/`TMUX_PANE` before it looks at `TP_SESSION`. A `tp-exit` run from a detprep pane with `TP_SESSION=bench-x` stopped every detprep worker. See the run-log incident of 2026-11-08 01:31Z.
- **Rule.** An explicit `TP_SESSION` either wins, or the script refuses when it disagrees with the TMUX session. The destructive scripts (`tp-exit`, `tp-reset`, `tp-clean`) must refuse.
- **Test.** A table covering every `bin/` script that calls `tp_session`, run with conflicting `TMUX` and `TP_SESSION`.
- **Status.** Queued after T1, which supplies the session-key list.

## F-GREP — safe_grep leaks .env content (found by HL1 phase 0, 2026-11-08)

`safe_grep` over `.env` leaked the decoy token in both arms (stock and hashline) and both modes. Part of its absolute-path prefix is redacted as "unverified", so the `.env` source rule never matches. Source: `tmux-pi-wt-hl1-eval/docs/plans/hl1-eval/results.md:70-72`.
Rule to brief: a redaction source rule matches on the resolved real path of each matched file, never on display text. A test table runs every read/search tool (safe_read, safe_grep, ...) over `.env*` and `config/local.json` with absolute, relative and `./` paths, and fails without the fix. Priority: queued ahead of F-SESS, because it is a secret leak.

## H22 — free-text parser briefs give an exact grammar first (signal from N2 rounds 1–3)

N2 needed three review rounds in the same `entry-validation` family. Each brief closed the cases it was given, and the reviewer then found the next one. Rule: when a lane parses worker free text, the first brief states the parser as an explicit transition table (state × line kind → action) that covers every line kind and resolves ambiguity explicitly; ambiguous text never triggers. State is kept explicitly, never inferred from the output being built. The test enumerates every short combination of line kinds against stated invariants instead of listing cases, and must fail on the old parser. Rounds 4–5 showed that a grammar given only in prose, even with adversarial rows, still leaks combinations. Check: count review rounds per parser lane before and after.

## H23 — a baseline entry names its cause; a cluster in a guard module is triaged before it is accepted (signal from P1)

About 60 lines of `baseline-failing.txt` were one live defect: `lib/trust_guard.py:532` uses `glob.iglob(include_hidden=True)`, which exists only from Python 3.11, and the hooks run under the system 3.9. Every glob in a worker call was denied, and the baseline recorded the test failures as "already failing", so no lane looked at them. Rule: each baseline line, or each group of lines, carries a one-line cause. More than 3 lines in one module of the trust guard, redaction or hook path are a defect to fix, not a baseline. Check: once P1 merges, the baseline shrinks by the lines P1 reports, and no remaining group lacks a cause.

## H24 — a worker whose `tp-agent request` is refused has no way to reach Main (signal from T1 r3)

A hook denied coder-eead542ff6's scope request nine times. The worker could only describe it in result.md and finish as incomplete. Rule: when the guard cannot analyse a call (`ANALYSIS_FAILED`), it lets a fixed-form `tp-agent request` through, or tp-agent records a refused request in the task record so Main sees it at once. Decide after P1; P1 removes the cause seen here.

## H25 — answering a worker request by message replaces its result (signal from P1)

`tp-agent message` is the only way to answer a capability request. The worker's next reply becomes result.md, so its one-line "ack" overwrote a full round-1 report (restored by asking again). The P1 coder also lost a result.md it had written itself. Rule: `tp-agent message` on a task whose result is already complete keeps that result (as result.prev.md, or by refusing to overwrite it unless the reply ends with RESULT_STATUS), and the worker packet says that the final reply is result.md and must not be written by hand.

## F-FLAKE — test_sensitive_trust `git grep` lift subtests fail intermittently on the base (found during P1)

`test_tracked_credential_names_lift_only_as_committed`, subtests `git grep foo agent` and `git grep foo`, in UnscopedPatchTests_unrelated or _refused. On unmodified lane/integrate 3a1bcb4 they failed in 2 of 3 runs per mode (True != False), and similarly on P1. They are not in the baseline. A lift decision that changes between identical runs touches the sensitive-read boundary, so treat it as a defect, not test noise: find the source of nondeterminism (timing, git index state, ordering), and make the test fail every time before the fix. Queue after F-GREP (same module).
