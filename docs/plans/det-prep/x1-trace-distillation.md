# X1: Strong-model traces to help a smaller model (harness only, no fine-tuning)

Status: **queued, design in progress** (owner request 2026-11-08). Run order: after the in-flight evals (RTK/H14) and alongside M1 (H15), whose model groups X1 reuses. Never before J1 control runs exist for the arms it compares.
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

## Design
Filled in after the inventory of the existing harness (bench runner, task records and traces, hooks/helpers/skills, model config, past Battle Soccer friction). See the sections below once written.
