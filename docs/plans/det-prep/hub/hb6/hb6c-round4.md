# HB6c round 4 (design): the view builds its own validated inputs; shared readers' defaults never reach a decision

Review review-coder-04464bd6ad-173059 found the unreadable-evidence family a third time. Read its report.md (in that task) in full. Its F1–F4 and the "Owed before close" items are this round's scope.

**Why rounds 2 and 3 missed.** Each round checked a file's outer shape and then handed the file to a shared reader: `contract.task_lifecycle`, `request_file.is_pending`, `Session._stamp`, or Session's step membership. Those readers fill in defaults: `{}`, an empty event list, pending, epoch 0, or `None`. The defaults are right for their own callers but turn into definite advice here. Listing more corrupt examples will not close this. The design changes instead.

## Design (keep it inside tools/waits/status.py)

1. **Lifecycle from validated inputs.** For the writer and the newest review, read every file in `lib/lifecycle_files.py` `LIFECYCLE_FILES` yourself. Build each argument of the pure `contract.lifecycle(...)` (lib/contract.py:505) and call it directly. Do not call `task_lifecycle` or `lane_steps.life_of`.
   - **Missing file:** a file that does not exist is absent. That is a normal state for a task that has just started.
   - **Existing file:** a file that exists must give every field `lifecycle` uses, with the right type, or the row is unknown. Check `state.json`'s `state` and `updated`, every checkpoint event's shape as `lifecycle` reads it, `completed` as a number, `exit`, `result.md` as text, and each request file in `REQUEST_NAMES`. An unreadable request file gives unknown, not pending.
   - **Marker files:** files used only for presence (escalated, stopped.json, collected.json, pending-capability.json) need only exist.
   - **Cross-check:** a table in report.md lists each `lifecycle` parameter, its file and field, the validation, and file:line in contract.py where it is used. A parameter missing from the table is a defect.
2. **Selection from validated metadata.** Before choosing the step's writer or newest review, check the selection fields of every task whose `meta.json` names this lane. The fields are `created`, `lane`, `lane_step`, `role` and `review-of`.
   - If any of them is missing, of the wrong type, or (for `created`) does not parse, the lane is unknown, with that task named.
   - Never let an unreadable `created` sort as oldest, and never let a malformed `lane_step` drop a task out of the step.
   - Leave Session and the lane gate unchanged.
3. **One problem path.** Every check above that fails adds `{where: <the record's path>, problem}` to the report's `problems`, in addition to giving unknown in `next`. Do not duplicate timeline's diagnostics for the same file.

## Tests

Extend `StatusTest.test_next_action_table`; add no new test method. The table has become slow (~24 s, about 30 lane worktrees), so add the new cases as **in-process rows on one existing lane and task, with sequential mutations** and no new worktrees. Cover:
- `state.json`: `{}`, `{"state":null}`, a non-text state, and a bad `updated`;
- `checkpoints.json`: `{"events":[3]}`, an event of the wrong shape, and `{bad`;
- `completed` `bad` and an undecodable `result.md`;
- `permission-request.json` / `capability-request.json` `{bad` (each gives unknown, not wait);
- a newer writer with `created` `[]`, then `"broken"`, then with `lane_step` `[]`;
- a newer review with `created` `[]` (it must not fall back to the older approval);
- a just-started writer with no optional files (still running);
- the existing readable permission beside a corrupt sibling (still rule 1).

Each row asserts both the rule or unknown result and the `problems` entry.

Fail-before: a `.py` in your evidence/ dir, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`.
- Run it against 774e7de **without editing any tracked file**: use `git worktree add` into a fresh dir under /tmp, or `git show 774e7de:tools/waits/status.py` into a temp module. The reviewer could not run your round-3 script because it rewrote the source in place.
- Show the F1 and F2 cases failing separately, with no earlier crash masking them.

## Owed (check)

Correct report.md's git inventory. It must include the `git --no-optional-locks status` calls (lane_commands.py:83 and :392) and the rev-list, symbolic-ref, cat-file and merge-base checks in lane_reality/_row, each confirmed as a read.

Run `tests.test_wait_timeline` and `tests.test_session_env` once through `tools/agent/test-run.sh`:
- use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground.

Report the timeline module's time against round 3's 25.4 s.

Touch only tools/waits/status.py and tests/test_wait_timeline.py. Do not touch the default tmux server. Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
