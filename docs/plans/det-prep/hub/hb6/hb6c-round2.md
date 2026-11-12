# HB6c round 2 (lane hub-status, on d9d6225)

Review review-coder-4a62113441-171238: REQUEST_CHANGES F1-F4 (read its result.md and report.md; it has repro.py). Main's
diagnosis: all four are real.
- F1 comes from the brief's "reuse the lane reader": the reused row runs the lane gate, which snapshots a dirty worktree into
  git objects.
- F2 and F3 are one rule the brief stated ("cannot be read reliably -> unknown") but did not spell out per record.
- F4 is a missed field.

## Rules (restate in report.md, then code to them)
1. **Read-only means no writes anywhere**, the repo's git object database and index included. Get the open step without the
   lane gate (`lane_steps.row_fields`/`evaluate`/`_tree_now` and anything else that snapshots). Use a read-only derivation from
   the lane record. If none exists, write the smallest one in status.py from the lane record's fields, and say which fields.
   List every git command status.py now runs (each must be a read).
2. **Unreadable evidence is never absence.** A task the session associates with the lane step through any authoritative
   source (`Session.unreadable_of`: dispatch.json or the lease) but cannot read makes the row `unknown` with the reason,
   whatever readable writers exist, including a newer unreadable task beside an older readable one. A malformed meta.json
   anywhere is listed under `problems`.
3. **Requests:** read each request record's id, kind, status and opened yourself. A record missing or misreading any of them is
   unreadable.
   - A readable open permission request decides (rule 1).
   - Otherwise any unreadable request record of the task makes the row `unknown`. Never pick rule 2 or rule 6 past it.
4. **Text mode shows every row field**, `objective` included.
5. **(F5, small)** In `next`, put the row's own values in place of TASK, REQ, LANE and STEP when they are known. Keep the
   placeholder when one is unknown.

## Tests
Extend StatusTest's existing table; add no new method beyond what the table needs. Add rows for:
- F1: one dirty finished lane. Assert that the object database (a listing of .git/objects) and the index are byte-identical
  before and after, and that the source and session files are unchanged.
- F2: dispatch-associated and lease-associated unreadable tasks, including the newer-unreadable-beside-older-readable case.
- F3: an id-only record; an open record missing kind; a valid capability request beside a corrupt sibling.
- F4/F5: render rows.

Use `tests/lane_fixture.py`'s git helper instead of the new wrapper (review F6). Show F1-F3's fail-before on d9d6225 in the
evidence-file form: a `.py` in your evidence/ dir, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`; the review's
repro.py is a fine start.

Run `tests.test_wait_timeline` and `tests.test_session_env` once each through `tools/agent/test-run.sh`:
- use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground.

Then rerun case 2 on the real session, and confirm with the same object-listing check that the repo gained no objects.

Touch only tools/waits/status.py and tests/test_wait_timeline.py. Do not edit bin/, config/ or claude/. Request through
`tp-agent request` and stop if you need more. Commit nothing. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
