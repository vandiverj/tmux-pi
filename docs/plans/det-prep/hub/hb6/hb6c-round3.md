# HB6c round 3 (rule first): every record the decision reads is checked before it counts

Review review-coder-1295aa07aa-172045 (report.md in that task) found the same family a second time: a record is unreadable or malformed, yet the view either crashes or picks a definite next action.
- **F1:** `_read_requests` (tools/waits/status.py:153–155) tests `value.get("status") in REQUEST_STATUSES`. A request with `"status": []` or `{}` raises TypeError, and the whole view aborts.
- **F2:** `lane_steps.life_of` returns a default (starting or working) for a malformed `state.json`. The view then shows rule 4 (worker, wait) or rule 5 (review running) as definite.

Round 1's rule stands: an unreadable condition gives unknown, never a guess. A row-by-row fix has now missed twice, so this round starts from the rule.

1. **Rule first (report.md).** List every input `decide` uses. For each, name:
   - the record and field it comes from;
   - the reader function, with file:line;
   - what that reader does with a missing field, a wrong JSON type (list, object, number, string, null) and an unparseable file.

   Mark each input as "checked → unknown" or "trusted default". Every "trusted default" that can reach a definite rule is a defect. Fix them all, not just F1 and F2.
2. **Fix inside the status view only.**
   - Validate every field's type before any membership test or comparison.
   - For lifecycle (writer and review), add a status-side readability check. A malformed `state.json` makes the row unknown. This applies to the writer's and the review's.
   - Leave `lane_steps`, `contract.task_lifecycle`, timeline and the lane gate unchanged. Their defaults are right for their own uses.
   - A task that has only just started, with optional markers not yet written, must still read as running, not unknown. Say which markers are optional and why.
   - A corrupt request beside a readable permission request must not stop the permission from deciding.
3. **Tests.** Extend the existing `StatusTest.test_next_action_table` with rows; do not add new test methods. Cover:
   - status `[]`, `{}` and `3`;
   - corrupt writer state.json;
   - corrupt review state.json;
   - just-started writer with no optional markers (still running);
   - corrupt request beside a permission request.

   Fail-before: put a `.py` in your evidence/ dir, run it as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`, and show it on the round-2 code (702c851) and after your change.
4. **F3 (note):** correct the git-command inventory in report.md. Add `git diff --name-only -z BASE..refs/heads/BRANCH` (lane_commands._overlaps → _measured) and `git check-ref-format` (lanes.py lane validation). Confirm both are reads.

Run `tests.test_wait_timeline` and `tests.test_session_env` once through `tools/agent/test-run.sh`:
- use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground.

Touch only tools/waits/status.py and tests/test_wait_timeline.py. Do not touch the default tmux server. Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
