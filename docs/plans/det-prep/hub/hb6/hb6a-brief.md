# HB6a: where work waits, built from existing records (lane hub-waits, WT1 arm A)

Owner (2026-11-10): "My priorities are understanding where work waits and avoiding repeated verification." HB6 = H39 = WT1 (H32);
plan `../../wt1-wait-timeline.md` (read it first: inventory, report shape, arms, measures, decision). This step is **arm A
only**: a report built after the fact from files that exist today. No reporter, hook, tp-agent or tp-spawn change (that is arm
B, HB6b). Bar: realistic use (`docs/plans/realistic-use-bar.md`), the owner's own sessions on this Mac.

## Rules first (write this table in report.md before code)
One row per record kind the report reads, with: path, which interval or instant it gives, state it implies, confidence
(exact = a written event time; derived = file mtime or transcript pairing; inferred = gap between known events). At least:
meta.json `created`; state.json (`state`, `tool`, `updated`, `promptLog[].at`, `completedTurn`); `exit`, `collected.json`,
`reaped.json`, `stopped.json`, `result.md` mtimes; `events.jsonl` (lib/hub_records.py, tp-event:v1: request.*, correction.*,
pause.*); `requests/` records (`opened`, resolution time); `capability-request.json` (`requested`/`answered`); test-run records
in `test-runs/` (`seconds`, `exit`, file time) and gate records (lib/gate_record.py `started`/`finished`); lane.json step
`closed_at` and the lease `taken`; a review task's `review-of`; the Claude worker transcript (path in `transcript-path`, or
`transcript.jsonl`): only `timestamp`, message role and tool_use/tool_result names and ids, never message text.
Then the derivation rules: how gaps become `unknown`; how a review interval makes its coder wait (`waiting:review`, add it to
the plan's states); how overlapping tasks count once in elapsed and per worker in accumulated; what "delayed completion" means
(the critical path from the first task's `created` to the last `collected`). Tool names through `hub_records.normalize_tool`;
no second table.

## Build
- `tools/waits/timeline.py`: `python3 tools/waits/timeline.py SESSION_TASKS_DIR [--task ID ...] [--json]`. Output per task the
  ordered intervals `{task, actor, state, start, end, wait_reason, source, confidence}`, the two totals (elapsed, accumulated),
  unknown share, and the critical-path interval with one suggested focus. Read-only: it never writes into the session dir and
  prints no transcript text, no file content, nothing from `.env*` or credential files. Missing or malformed files make that
  piece `unknown` and are listed; they never crash the report.
- A task still running has an open last interval ending "now" (flagged), not a guessed end.

## Case 1, controlled delays (ground truth)
Build producer-shaped task dirs in a temp dir (copy the field shapes from report.sh, reporter.ts, test-run.sh, gate_record.py and
a real task dir; say which you copied) with a planted schedule: a 90 s test run, a review that holds its coder, a request open
for owner input, a queued `--after` child, a worker that never takes its first prompt (H9 stall), one killed mid-tool, two
overlapping tasks. Score attribution per second against the plan. No live models, no tmux needed for arm A.

## Case 2, real runs (read-only)
Run the report on this Mac's real task dirs: the SG1 rounds (lane safe-search), product-design PD-A..F, and today's HB8b rounds
(lane hub-episodes2). Report per run: unknown share, the critical-path interval, and any case where the timeline changes the next
action (state the action before and after). Quote no transcript text.

## Attack table (one table in report.md, not one test per row)
Input space: harness {claude, pi} x record present/missing/malformed for each row of the rules table x task {finished, running,
stopped, reaped} x timing {overlap, clock order between sources disagrees by up to 2 s, mtime older than created}. For each class:
expected output and the row that proves it. Prove fail-before where a rule replaces an obvious naive reading.

## Tests and verification
One new module `tests/test_wait_timeline.py` (a new tool needs one): one table-driven method for the rules plus the case-1
scoring, using temp dirs. No ritual tests. Run it and `tests.test_session_env` once through `tools/agent/test-run.sh` with
`TMUX_TMPDIR=$(mktemp -d /tmp/h6w.XXXX)`. Report the WT1 measures you can compute from arm A (attribution accuracy, unknown share,
stall visibility delay) against the plan's thresholds and say which decision they point to (keep on demand / need arm B).

Report in report.md. Do not commit. Do not add git notes.
