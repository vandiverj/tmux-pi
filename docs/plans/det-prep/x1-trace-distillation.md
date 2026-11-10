# X1: Strong-model traces to help a smaller model (harness only, no fine-tuning)

Status: **queued; design written, blocked on J1 bench** (owner request 2026-11-08). Run order: after the in-flight evals (RTK/H14) and alongside M1 (H15), whose model groups X1 reuses. Never before J1 control runs exist for the arms it compares.
State survives compaction here and in `run-log.md`; per-run rows go to `loop/results.tsv` with hypothesis `X1-<candidate>`.

## Owner's requirements (binding; condensed from the request)
- **Objective:** find out whether verified traces from a stronger model can raise smaller-model task success through better tools, deterministic helpers/hooks and selectively loaded procedural skills. No fine-tuning. Establish which interventions help, why, and whether they generalize.
- **Scope/isolation:**
  - Inspect and reuse existing harness, runner, hooks, helpers, skills and results first.
  - Use the campaign's configured models. Record exact model, inference settings, context limit and runtime versions.
  - All experimental code lives in isolated worktrees or opt-in experiment config: nothing merged, globally enabled, made default or adopted. Output is a reviewable candidate plus a recommendation.
  - Don't touch unrelated app code or disturb other runs.
- **1. Tasks:**
  - Mine real Battle Soccer friction and failures, plus successes.
  - Families: environment/branch/lane/task-context discovery; source navigation and finding every instance of a defect; tool choice, arguments and shell invocation; choosing and running verification; evidence-backed completion reports. Use real coding tasks, not isolated tool drills.
  - Split dev / validation / untouched holdout. The holdout varies files, instances or conditions, not wording. Related tasks stay in one split.
  - Acceptance criteria, budgets and promotion criteria are fixed before any tuning.
- **2. Evidence per run:**
  - starting commit and workspace state;
  - model, harness, tool, skill and config versions;
  - task packet and instructions;
  - tool calls (arguments, results, durations, failures, retries);
  - hook interventions, automatic argument corrections and helper-internal operations;
  - final diff, verification evidence, reviewer findings and escalation.
  - Model explanations are hypotheses, not proof.
  - Failure classes: missing context, ambiguous tool choice, invalid arguments, execution/environment, stale state, incorrect reasoning, incomplete scope, insufficient verification. A correctly invoked test that finds a defect is not tool misuse.
- **3. Interventions:** try them in this order:
  1. facts in the packet;
  2. simpler tool descriptions, schemas, defaults or outputs;
  3. a deterministic helper;
  4. a hook for mechanical work;
  5. a short procedural skill.
  - Shared logic goes in a lib with thin adapters, because Claude hooks don't cover Pi.
  - Each skill states applicability, prerequisites, expected results, verification and supporting run ids, with instance answers stripped.
  - A few targeted interventions only: no new framework and no broad skill catalog.
- **4. Comparisons:**
  - Arms: A small model with the current harness; B tool/deterministic intervention only; C skill only; D both. Strong model with the current harness is the reference where no comparable run exists. Extra configurations are labelled, and an arm that doesn't apply gets a written reason.
  - Matched inputs, starting states, budgets and config. Reset the workspace per trial, record cache conditions and randomize order.
  - Pilot first, then repeats. Report sample sizes and uncertainty; one success is not an improvement.
  - Run ablations and don't bundle changes.
- **5. Metrics:**
  - raw first-call validity before correction, and the auto-correction rate;
  - appropriate-tool rate and useful-result rate;
  - repeated reads, searches and failure signatures;
  - accepted completion without escalation;
  - review findings and repair rounds;
  - elapsed time;
  - input, output and cache tokens, and cost;
  - helper/hook time and internal work.
  - Cost includes prep, skill loading, summarizing, retries, review and frontier rescue. One-time development cost is reported separately. Fewer visible calls is not less work.
- **6. Verification:**
  - Use task tests, regressions, static checks and artifact/state checks. "Done", a valid call or exit 0 is not enough.
  - Graders and protected tests sit outside the candidate's editable scope. Changes to app tests are reviewed for weakening.
  - Evidence is bound to the final workspace state and invalidated by later edits.
  - Adverse conditions: stale context, changed files, unavailable tools, malformed arguments, hook failure, incomplete evidence. Hooks must not loop, silently change intent or rerun expensive checks.
  - Use the campaign's targeted and full-suite policy.
- **7. Report:**
  - a table by intervention × model × task family;
  - reproducible configs and artifacts;
  - failures, regressions, exclusions and limits;
  - observed vs causal claims;
  - a per-candidate verdict: reject / revise / promising-inconclusive / ready for review;
  - the isolated diff and config, plus rollback.
  - Advance only on held-out evidence. The holdout stays untouched until selection; if it informs tuning, it becomes dev data and a fresh holdout is needed.
  - **Stop before adoption and present to the owner.**

## Inventory (2026-11-08; what exists, what X1 reuses)
- **No bench runner yet.** `tp-bench`, `lib/bench/` and `bench/` are only specified (`j1-benchmark.md`): battle-soccer worktrees at tag `bench-base-v1` (`dfea0c8`), task kinds code/review/local/orient, sealed answers in `~/.local/share/tp-bench/answers/`, results in `bench-results/<label>/<n>.json`, `compare` with the PROTOCOL keep rule. **X1 depends on J1 being built and its control recorded.** X1 adds task specs and one trace report; it never changes the scorer (PROTOCOL "Off-limits").
- **Battle-soccer history.** tmux-pi has never run on battle-soccer: V-A..V-G are not run and no task meta names it. Prior battle-soccer runs exist in `~/Documents/Projects/pi-improvement-workbench`:
  - `src/workbench/battle-soccer-cycle.ts` has 10 feature tasks, 01_accuracy_summary … 10_refactor_placement_tests. It records per-task score, tool calls, tokens by phase and tool failures.
  - The results are in `Agent Thoughts/strategy/run-sdlc-improvement-loop-ledger.md`.
  - battle-soccer also has its own `docs/process-improvement/friction-log.md` and `events.jsonl`.
  - These, plus the det-prep run log and `fixes/*-round*.md` (multi-round missed-site families), are the friction to mine.
- **Traces already captured:**
  - Pi workers write `transcript.jsonl` in the task dir. Each tool call has name and arguments; results carry `isError`, `durationMs` and a timestamp; usage is per message.
  - Claude workers: `transcript-path` points at the Claude Code jsonl, with tool_use/tool_result and usage. There is no per-tool duration (take it from timestamp gaps, or from H16 OTel if that eval lands).
  - `tp-improve routines` (`lib/routines.py`) already parses both formats per role.
  - `tp-evals families` (`lib/task_family.py`) groups attempts, rounds and tokens.
- **Existing interventions** (most on `lane/integrate`), each as layer and kind:
  - lane facts in the packet (C1) — packet;
  - `--context file#Heading` (C2) — packet;
  - Tools block from `.tmux-pi/agent-tools.json` (A1; battle-soccer has none yet) — packet/tool description;
  - `tools/agent/lane-info.sh` — helper;
  - `tools/agent/test-run.sh` + gate record with tree fingerprint (B1) — helper/hook;
  - `--tests affected|full` — packet;
  - `lib/family_sweep.py` `sweep.md` for retries — helper;
  - tracked `git grep` passes without a prompt (K1) — hook;
  - `safe_read`/`safe_grep` (Pi extension + Claude MCP) — tool;
  - claims-last review (D1) — review.
  - The only argument rewrite is `lib/redact_hook.py` (appends `|| echo exit-code: nonzero`; it stalled Haiku, H10) and it is not logged.
  - There are no procedural worker skills (`skills/make-qa` only), and workers run `--no-skills`.
- **Models** (`config/model-catalog.json`):
  - local Qwen `lmstudio/qwen/qwen3.6-35b-a3b` (catalog context 131072, loaded at 65536 per G1, max_output 32768, thinking off, router at temperature 0, seed 7);
  - small = Haiku 5.5 / GPT-6 luna;
  - coders default to mid (Sonnet 5.5) with `strict_floor`.
  - Launch: `bin/tp-claude` (isolated `CLAUDE_CONFIG_DIR`, plugin hooks); `bin/tp-pi` (`agent/` dir, explicit extension list, Node pinned by `TP_PI_NODE`).

## Design

### Arms and models
- **Small models under test:** S1 = local Qwen via Pi (primary; cheap, so it carries the repeats); S2 = Haiku 5.5 via Claude (frontier small; fewer repeats). The `strict_floor` mid coder floor is overridden only inside the experiment config, never in defaults.
- **Reference R:** Sonnet 5.5 via Claude with the current harness. Its traces are the "stronger model" source, and only dev-split traces are mined.
- **Arms per candidate:** A current harness; B tool/deterministic only; C skill only; D both. An arm that does not apply (for example a candidate with no skill component) is recorded as n/a with the reason. M1 profiles (H15) are off in all X1 arms unless labelled as an extra configuration.
- **Isolation:**
  - worktree `../tmux-pi-wt-x1`, branch `exp/x1` from `lane/integrate`;
  - every intervention is gated by `TP_X1_ARM=A|B|C|D` (read by thin adapters in `bin/tp-claude` and `bin/tp-pi`, with shared logic in `lib/x1/`);
  - skills load only in C/D, from `exp/x1/skills/`;
  - battle-soccer's `.tmux-pi/agent-tools.json`, if a candidate, is written into the per-trial worktree, never the battle-soccer checkout.
  - Rollback = remove the worktree/branch; no defaults change.

### Run record (added to what tasks already capture)
`x1-run.json` per trial is written by the shared lib after the run, never by the worker. Fields:
- start commit, `git status --porcelain` hash and tree fingerprint;
- arm and `TP_X1_ARM`;
- model id, provider, level, temperature/seed/thinking, loaded context length;
- `claude --version`, pi version, Node and LM Studio model build;
- tmux-pi commit and the sha256 of each enabled hook/extension/skill/helper;
- packet sha256;
- the normalized tool-call list from the transcript: tool, args hash plus args, ok/error, result bytes, duration, retry-of, and **raw args before any hook rewrite**;
- the hook-event log: each hook writes `hook-events.jsonl` (hook, action, before→after, ms), redact_hook included;
- helper-internal ops (helpers log their own sub-steps and time);
- final diff, test-run records, review id and findings, escalation and rescue;
- tokens (input/output/cache) and cost by phase: prep, skill load, work, retries, review, rescue;
- cache state (fresh session, prompt-cache warm or cold) and trial order index.

### Failure classes (from observable events; model explanations are only hypotheses)
`missing_context`, `ambiguous_tool_choice`, `invalid_arguments`, `exec_env_failure`, `stale_state`, `incorrect_reasoning`, `incomplete_scope`, `insufficient_verification`, `none`. A correctly invoked test that finds a defect is `none` for tool use. Two classifiers work blind to the arm: the rule-based trace normalizer first, then one reviewer from a different provider for the classes that need judgment. Disagreements are listed, not resolved silently.

### Tasks (authored in X1-1, before any tuning)
- Families: F-env (env/branch/lane/task-context discovery), F-nav (find every instance of a defect), F-tool (tool choice and argument/shell construction), F-verify (choose and run verification), F-report (evidence-backed completion report). Every task is a real battle-soccer coding change, and each exercises 2+ families.
- Sources: the 10 workbench tasks (their recorded failures and successes), battle-soccer `friction-log.md`, `events.jsonl`, and the multi-round sweep families from det-prep, re-expressed as battle-soccer defects (for example a defect pattern planted at N sites in `src/game`).
- Size: about 18 tasks, 6 dev / 5 validation / 7 holdout. Related tasks (same module, same defect pattern, same workbench parent task) stay in one split. Holdout tasks use different files, a different number and placement of instances, or different conditions: lane vs no lane, a stale packet, or a renamed module. They are never a rewording of a dev task.
- **The holdout is authored and sealed by a separate agent** that sees only the family definitions. Main and the tuning steps never read holdout specs, traces or answers until X1-7. If any holdout result informs a change, those tasks become dev and a fresh holdout is authored.

### Acceptance (per trial, fixed now)
All of these must hold:
1. hidden vitest tests pass (sealed, outside the editable scope);
2. a protected regression subset of battle-soccer tests passes;
3. `tsc --noEmit` and the repo lint are clean;
4. the diff stays within the task's allowed paths;
5. any edit to existing app tests is reviewed and no assertion is weakened (an automatic diff check on removed/loosened `expect`, plus the reviewer);
6. every claim in the completion report is backed by a B1 test-run record whose tree fingerprint equals the final tree (an edit after the run invalidates it);
7. no escalation or frontier rescue. If rescue happened, the trial counts as "accepted with rescue", which is scored separately and is not success for the small model.

### Budgets (fixed now)
- Per trial: wall 25 min for S1, 15 min for S2/R; 100 tool calls; S2 at most 400k input tokens. Hitting a budget is a failure, `budget`.
- Campaign: frontier worker runs at most 70 (R and S2 together, excluding reviews). Local runs are uncapped but logged. Development cost (authoring, diagnosing, building candidates) is reported separately from trial cost.

### Metrics
- accepted completion without escalation (primary);
- raw first-call validity (before any rewrite) and the auto-correction rate;
- appropriate-tool rate (matches the family's expected tool class) and useful-result rate (result used in a later step, or non-empty and non-error);
- repeated reads, searches and failure signatures (same normalized args or error within a trial);
- review findings and repair rounds;
- wall time;
- tokens and cost per accepted task, all phases included;
- helper/hook ms and internal op counts. Work is counted as visible calls plus helper-internal ops, not visible calls alone.

### Promotion (fixed now)
- **Ready for review:**
  - On holdout (S1, at least 3 repeats per task per arm; S2 at least 2), the best arm beats A on accepted completion, with a 90% paired bootstrap CI over tasks that excludes 0.
  - No task goes from pass in every A repeat to fail in every candidate repeat.
  - Review findings per accepted diff do not rise.
  - Cost per accepted task (all phases) does not rise more than 10%. A pure-efficiency candidate needs equal accepted completion and at least 15% lower cost.
  - The ablation shows which component carries the gain.
- **Promising but inconclusive:** the point estimate is at least +15 pp but the CI includes 0, or the gain appears on validation only.
- **Revise:** a gain with a correctness regression in any family, or with hooks that loop or rewrite intent.
- **Reject:** otherwise.

### Interventions (built in X1-4, only from diagnosed obstacles)
For each obstacle class that recurs in at least 2 dev tasks or 3 trials, take the first rung that removes it: packet fact → tool description/schema/default/output → helper → hook → skill.
- Shared logic goes in `lib/x1/`, with adapters as a Claude plugin hook and a Pi extension.
- A skill is at most about 40 lines. It states applicability, prerequisites, steps, expected result, verification and supporting run ids, with instance answers removed.
- Target at most 4 candidates.
- Pre-diagnosis guesses, for checking only: battle-soccer `agent-tools.json` (test and lint commands), test-run summary output, a sweep helper (links H13), raw-arg logging plus a fix for the H10 redact rewrite, a verify-choice skill, and a completion-report skill.

### Adverse-condition checks (per candidate, before repeats)
- stale packet (base sha moved);
- a file changed under the worker;
- a helper/tool unavailable (binary removed);
- malformed arguments;
- a hook that exits non-zero or times out;
- an incomplete evidence record.
Each check passes when the worker is not stuck in a loop (no more than 2 identical retries), intent is not silently changed (every rewrite is logged and visible in the result), and no expensive check runs more than once per tree fingerprint.

### Phases (state in this file; rows in `loop/results.tsv` as `X1-<cand>`; log in `run-log.md`)
- X1-0 **prereq:** J1 bench built, sealed and its control recorded. Normalizer + `x1-run.json` writer built and unit-tested in `exp/x1`.
- X1-1 **tasks:** dev/validation authored; holdout authored and sealed by a separate agent. Criteria above frozen (this file's commit is the freeze point).
- X1-2 **traces:** R × dev × 2; S1 A × (dev+val) × 2; S2 A × dev × 1.
- X1-3 **diagnosis:** classify, then a table of obstacle × family × frequency, with supporting run ids.
- X1-4 **candidates:** build ≤4, each with unit tests and an independent review (other provider).
- X1-5 **pilot:** 1 trial per arm on 2 dev tasks, to check instrumentation and the adverse conditions.
- X1-6 **validation:** S1 arms A–D × val × 3, randomized order; S2 on the most promising × 2. Prune.
- X1-7 **holdout, once:** survivors only.
- X1-8 **report:** a table by intervention × model × family, configs and artifacts, failures, exclusions, observed vs causal, a verdict per candidate, the isolated diff and rollback. **Stop for the owner.**

### Open risks
- J1 is not built. X1 cannot start before it and must not build its own scorer.
- Qwen at 65536 loaded context may truncate long packets. Record the truncation and don't treat it as a model failure.
- The overlap with M1 (per-model profiles) and H13/H14/H16: X1 arms keep those off unless an extra configuration is labelled.
