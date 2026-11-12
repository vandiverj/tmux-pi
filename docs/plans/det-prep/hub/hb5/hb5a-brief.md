# HB5a: where verification repeats, from existing records (lane hub-verify, VR1 arm A)

Owner (2026-11-10): "My priorities are understanding where work waits and avoiding repeated verification." HB5 = H38; its
evidence layer is VR1 (`../../vr1-verification-reuse.md`: read it first: inventory, equivalence key, never-reused list, arms,
measures, decision). HB6a did the same first step for waits (`../hb6/hb6a-brief.md`, `tools/waits/timeline.py`). This step is
**arm A's measure only**: a read-only report, built after the fact, of which checks ran more than once on the same content. No
change to test-run.sh, gate records, review, lane close or tp-agent (the shared fingerprint and `tp-check` are later steps).
**Adoption is the owner's** (VR1: "Advisory"). Bar: realistic use (`docs/plans/realistic-use-bar.md`).

## Rules first (write this table in report.md before code)
One row per record kind the report reads, with path, the fields it uses, what makes two runs "the same check", and what it
cannot know. At least: `tp-test-run:v1` (`TP_TASK_DIR/test-runs/<n>.json`, tools/agent/test-run.sh:111-136); `tp-gate:v1`
(lib/gate_record.py; find where gate records are written); `tests.jsonl` from tests/run_bounded.py (no tree: say what that
allows); review tasks' `review-of` and their own test-runs; a task's lane and step (meta.json). Then the derivation rules:
- **Same check, same content** = equal canonical `cmd[]` (normalise only what VR1's key allows: cwd relative to the repo top,
  the interpreter path) and equal `tree`. Name each normalisation and why it is outcome-neutral.
- **Covered by an earlier run** = the module or test set of run B is a subset of run A's on the same tree with the same
  runner and interpreter (partial duplicate; report the seconds of B that A already covered, estimated from A's per-module
  times when the log has them, else marked `estimated` or `unknown`).
- **Never counted as a duplicate** (VR1's never-reused list): live model/LM Studio, network, live tmux, timing-sensitive,
  bench/smoke. Say how each is recognised from the record (env switch in cmd, module name, label) and list the ones you cannot
  recognise.
- A failed or timed-out run (exit != 0, 124) followed by a rerun on the same tree is a **retry**, not a duplicate; report it apart
  (it is the flaky/infra signal).
- Who ran it: worker (coder), reviewer, Main. Main's own verification today runs outside test-run.sh (no record); the report says
  so and counts what it can see. Do not guess Main's runs.

## Build
- `tools/checks/repeats.py`: `python3 tools/checks/repeats.py SESSION_TASKS_DIR [--lane L] [--json]`. Output: groups of same-check
  runs (task, role, label, cmd, tree, exit, seconds, record path), partial-duplicate pairs, retries, per lane and step the seconds
  spent on repeats vs first runs, and the share of all recorded test seconds that were repeats. Read-only: never writes into the
  session dir, prints no log contents and nothing from `.env*` or credential files. Missing or malformed records are listed and
  skipped; they never crash the report. Tool names through `hub_records.normalize_tool` if it needs any; no second table.

## Case 1, planted (ground truth)
Temp task dirs with producer-shaped records (copy the field shapes from test-run.sh and gate_record.py; say which) and a planted
schedule: coder runs modules X+Y on tree T; reviewer reruns X on T (duplicate); Main-like record reruns X+Y+Z on T (covers both);
a run on tree T' after an edit (not a duplicate); a fail then pass on T (retry); a live-model test on T twice (never counted);
same cmd with a different interpreter (not a duplicate unless the key says so); a malformed record. Score every pair.

## Case 2, real runs (read-only)
Run the report on this Mac's real task dirs (`.tmux-pi/sessions/*/tasks`; about 340 test-run records in 95 tasks today):
per lane, repeated seconds and share; the top 5 repeated checks; and the case VR1 names first (Main's cell runs repeating the
worker's on the same tree), stated as "not visible in records" with the count of rounds where it happened if the run log says so.
Quote no log text.

## Attack table (one table in report.md, not one test per row)
Input space: record kind {test-run, gate, run_bounded} x field {present, missing, malformed} x relation {same cmd+tree, same cmd
other tree, subset modules, other interpreter, other cwd spelling, fail-then-pass, never-reused kind} x role {coder, reviewer,
Main-visible}. Expected output and the row that proves it. Fail-before where a rule replaces an obvious naive reading (for example,
counting a retry or a different interpreter as a duplicate).

## Tests and verification
One new module `tests/test_check_repeats.py` (a new tool needs one): one table-driven method for the rules plus the case-1
scoring, using temp dirs. No ritual tests. Run it and `tests.test_session_env` once through `tools/agent/test-run.sh` with
`TMUX_TMPDIR=$(mktemp -d /tmp/h5a.XXXX)`. Report VR1's measures you can compute from records (duplicate executions, elapsed time
in repeats, retries) and say which next step they point to (shared fingerprint, `tp-check`, recording Main's runs).

Report in report.md (rules first). Do not commit. Do not add git notes.
