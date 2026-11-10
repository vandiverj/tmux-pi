# F1 round 4 (route: design) — fix each family by its rule, prove the rule with a table

Rounds 1–3 kept finding new cases in the same three families, because each fix covered only the case the reviewer cited. This round fixes the rule behind each family and proves it with a test that covers the whole rule.

## 1. retirement-dependency-order: rollback restores exactly what was there (lib/task_record.py, archive_task and reconcile)
- Before reconciling a task, take a snapshot of its directory: the set of file names and the bytes of every file that reconciliation may write.
- If reconciling fails:
  - delete every file that was not in the snapshot;
  - write back every snapshotted file whose bytes changed;
  - undo the rename.
- Don't keep a hand-maintained list of files to restore.
- Test: one parametrised test that, for each writer reconciliation calls (`write_record`, the `meta.json`, `task.md` and `request.md` writers, and any other you find), makes that writer fail.
- Assert that the task dir afterwards is byte-identical to the snapshot taken before: the same names and the same bytes. Include a legacy task with no `request.md`.

## 2. unreadable-meta: a pane id is `^%[0-9]+$` and nothing else (lib/task_record.py:1011-1014 and every reader of a recorded pane)
- A recorded pane that is not a string, or doesn't fully match `^%[0-9]+$`, is unknown. The task is kept, and tmux is not asked.
- Missing or empty still means gone, as today.
- Put the check in one function, and use it at every site that reads a recorded pane. Report those sites in the sweep table.
- Test: a table of `%7` (valid), `%bad`, `%7oops`, `7`, `% 7`, `%`, `%-1`, `""`, `None`, `7` (as an int) and a list. Assert keep or gone for each, and that tmux is never called for an unknown value.

## 3. display-order: navigation goes by meaning, not display position (bin/tp-status:161-175,499-504, and every consumer of row order)
- Find every consumer of the row order that `lib/tp_window.py` produces: `g`, `G`, `c`, selection after a repaint, and the footer counts.
- For each one, decide what it means: the oldest finished task, the newest task, and so on. Compute that from the task's timestamps, not from its index in the rows.
- Test: a table of layouts with an active row first, an active row last, and all finished rows. For each layout, assert which task `g` and `G` select, and what each of the other consumers selects.

## Report
- For each module (`test_tp_clean`, `test_tp_window`, `test_status_repaint`): the command, the test count, the seconds and the log path (output redirected to `$TP_TASK_DIR/tests.log`).
- Show the new table tests fail on the round-3 code: use a copy of the old files, and quote the failure counts.
- A sweep table of every site in each family.
- Don't edit README.md: lane local-model owns it. Note any needed wording instead.
- Do not commit.
