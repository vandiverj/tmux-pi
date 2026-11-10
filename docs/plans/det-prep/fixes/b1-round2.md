# B1 round 2 — fix each finding by its rule and prove the rule

Reviewer: review-coder-2dc7b5f969-162309 (report.md in that task dir). Four code findings and one check are owed. Each fix below states the rule, and its test must cover the rule, not only the cited case.

1. F1 — the gate fails closed on any step it could not complete (`tools/agent/pre-commit-gate.sh:363-372`, and every other step in that script that copies, lists or runs).
   - A failed copy, a failed listing, or a missing file that was collected fails that check with a reason. It never passes on a partial set.
   - Sweep the whole script for ignored errors: `|| true`, unchecked `cp`, `mapfile` from a failing command, and pipes without `pipefail`.
   - Test: a table of failure injections, one per step found. Each must give the check `failed`, not `passed`.
2. F2 — a check passes only if it ran in every app that has changes (`tools/agent/pre-commit-gate.sh:224-253`).
   - Record the result per app. The check's status is `passed` only when every changed app passed. A missing tool in any changed app is `missing` (a failure under `--require`).
   - Test: the reviewer's two-app Prettier case, plus all-present, all-missing, and one app unchanged with its tool missing (which must still pass).
3. F3 — a record is valid only if every required field for its status is present and well typed (`lib/gate_record.py:36-61`).
   - Define the required fields per status in one table in `lib/gate_record.py`: for example `passed` and `failed` need `exit` (an int) and `seconds` (a number). `validate` uses that table.
   - `status()` treats an invalid record as `invalid`, never `fresh`.
   - Test: for each status, drop each required field in turn and assert `invalid`.
4. F4 — record numbers are taken atomically (`tools/agent/test-run.sh:101-119`).
   - Claim the number with an exclusive create: a `mkdir`-based lock, or `set -o noclobber` with a retry loop on the next number. Write the record, then rename it into place.
   - Test: start 8 runs at once on one `TP_TASK_DIR` and assert 8 distinct records, 1–8, with none lost.
5. Owed check: show that the tests for acceptance items 1–5 fail on the unchanged base.
   - Run them against `lane/integrate` HEAD, or a copy of the old files, and quote the failure counts.

## Report
- For each module (`test_gate_record`, `test_agent_tools`, `test_preamble`): the command, the test count, the seconds and the log path (output redirected to `$TP_TASK_DIR/tests.log`).
- A sweep table.
- Do not commit.
