# PD-B round 5: a staged record that is present is validated as it is (Main, 2026-11-10)

Review review-coder-317cadc133-223312 (REQUEST_CHANGES). Report, `probes_r4.py` and both interpreters' results are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-317cadc133-223312/`.
Run `probes_r4.py` on both interpreters before you change anything. Round 4's prospective-state builder, the one encode gate,
the round-3 probes and both attacks (792 / 10,404 cases) were confirmed; do not rework them.

## The defect
- **F1:** interrupt apply (also adopt and ingest) after its commit point, replace the staged `commit.json` with JSON `null` and
  update the journal's `new_commit` digest to match. `--recover` succeeds, publishes `null`, deletes the journal, and the next
  `status` reports `control.invalid` (store.py ~428-433, 789). `prospective()` reads an explicit JSON `null` as "no record" and
  validates a record it synthesizes instead.

## Main's decision
**Absent and present are decided by the file, never by the value.** A staged or live control file that exists is parsed and its
value, whatever JSON it is (`null`, `false`, `0`, `""`, `[]`, `{}`, a wrong-typed object), is validated as that record by the
same function `status` uses. Only a file that does not exist is "absent", and only where the format allows absence. One helper
reads a control file and returns either "absent" or the parsed value; every reader of every control file (history, commit record,
manifest, txn, package documents) uses it. No reader turns a falsy value into absence (`or {}`, `if not x`, `.get(...) or`).

## Attack the rule before you report
Each control file (staged and live: history, commit record, manifest, txn, each document) × each JSON value (null, false, true, 0,
1, "", "x", [], [null], {}, an object of the right shape with one field null) × present/absent × command (apply, adopt, ingest,
recover, status, validate) × interrupt point (before and after the commit point). Expected: nothing published that `status`
then refuses; a refusal keeps the journal and names it; no exception. Print the input space by code, run it on both
interpreters, report counts. Keep worth-keeping rows as tests and fold `probes_r4.py` in. Also sweep the code for every
falsy-to-absent pattern on a control value and list each with its disposition.

## Report
- Fail-before for each new test against 8c2436d (the round-4 checkpoint).
- The four-cell run (`tests.test_product_design_package tests.test_product_design_store tests.test_skill_catalog
  tests.test_session_env`): /usr/bin/python3 and /opt/homebrew/bin/python3, each outside (`env -u TMUX -u TMUX_PANE -u TP_SESSION
  -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`env TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`), after your last edit, one
  cell after another. Each log starts with `date -u` and `git add -A && git write-tree` of the worktree; each file's time per cell.
- Attack counts, the review's probes rerun, Self-review, sweep, deviations.
- Commit nothing; `git add` new files. Brief rules (pd-brief.md) still apply. `RESULT_STATUS: complete` as the last line.
