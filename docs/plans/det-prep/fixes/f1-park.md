# F1 (status-cleanup) parked after round 3

Round 3 reviewer (review-coder-222f5202c9-161245): REQUEST_CHANGES, three code findings.
- F1, family retirement-dependency-order: after reconciliation fails, the rollback at `lib/task_record.py:836-858` restores only `meta.json` and `task.md`. A `request.md` created by `write_record` is left behind.
- F2, family unreadable-meta: the pane check at `lib/task_record.py:1011-1014` tests only the `%` prefix, so `%bad` or `%7oops` count as pane ids.
- F3, family display-order: rows are now active-first, but `g` and `G` in `bin/tp-status:161-175,499-504` still jump to the first and last displayed rows, not the oldest and newest finished.

Rule-level fixes for a round 4 if the owner wants one:
1. Snapshot the task dir (the list of files plus their bytes) before reconciling, and restore exactly that snapshot. The test must make every writer fail in turn.
2. A pane id must match `^%[0-9]+$`. Test it with a table of malformed ids.
3. `g` and `G` choose by age, not by display position. Test with an active row first and an active row last.

State: uncommitted in ../tmux-pi-wt-status-cleanup. 28 + 40 + 45 tests OK. Battle-soccer V-F is still owed.
