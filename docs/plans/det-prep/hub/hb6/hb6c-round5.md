# HB6c round 5 (design): no record can abort or erase the view; isolation is structural

Review review-coder-8722b458eb-175249 found the same unreadable-evidence family a fourth time. Read its report.md (in that task) in full. Its F1–F3 are this round's scope.

The remaining cases are not new schema rules. They are exceptions that escape: RecursionError from deeply nested JSON, OverflowError from `math.isfinite(10**400)`, and UnicodeDecodeError from a 0xFF byte in `review-of`. Each one happens in a reader that runs before the status checks. One aborts the whole view, and another returns `lanes: []`.

Another list of exception types would miss the next one. The design closes the class instead.

## Design (tools/waits/status.py only; shared readers unchanged)

1. **Lanes come first, from lane records alone.** The set of open lanes comes from the lane records, read as now. It never depends on Session or timeline having been built. If Session or timeline cannot be built, every open lane is still a row, with `who` unknown and the failure under `problems`. `lanes: []` happens only when there are no open lanes.
2. **Per-lane isolation.** Build each lane's row inside its own boundary. Any exception while gathering that lane's evidence gives that lane unknown. This covers selection, timeline lookup, lifecycle, requests and the review with its verdict sidecars. Its problem names the record path when known, otherwise the lane, plus the exception class.
   - Other lanes are unaffected.
   - `KeyboardInterrupt` and `SystemExit` still propagate.
   - Say in report.md why a broad catch is right here: the view advises and never acts, and an unknown row is always safe.
3. **Per-record isolation where a shared reader is global.** If one task's record makes a session-wide reader raise (Session's metadata or review-of read, `_problem_dirs`), find out which task it was where you can. List that task's path, and mark unknown only the lane or lanes it could belong to. A task whose lane cannot be read makes every open lane unknown, as now.
4. **Total numeric predicate.** `_number` (and anything like it) must never raise for any JSON value. A huge integer either counts as finite (compare it as an int, without converting to float) or is out of range and so unreadable. Choose one and give the reason.

## Tests

Extend `StatusTest.test_next_action_table` as in-process sequential rows on the existing lane and task. Add no new method, worktree or CLI process. Cover:
- **F1:** a 0xFF byte in a writer's and in a review's `review-of`. The lane is kept, it is unknown, and the path is listed.
- **F1:** deeply nested JSON (`'['*1100 + '0' + ']'*1100`) in meta.json, writer state.json, a request record, and a review's acceptance-items.json. The lane is kept, it is unknown, and nothing raises.
- **Lane isolation:** with two open lanes, one bad record in lane A leaves lane B's definite rule unchanged.
- **F2:** `tools: 10**400` in state.json and in a checkpoint event. Nothing raises, and the result follows the rule you chose.
- **F3:** one row each for state.json `tools` and `completedTurn`, checkpoint event `turn`, an empty `exit`, `requests` as a file, `requests` as a symlink, a present non-text `role`, and a malformed present `review-of`. A missing `role` or `review-of` stays valid, as Main ruled in round 4.

Fail-before: put a `.py` in evidence/ and run it as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py` against 88040f4. Extract that tree to /tmp first, and do not edit any tracked file. The script must **exit nonzero on any mismatch**. Round 4's attack.py exited 0 on mismatches.

## Report

For each case, tag it **realistic** (it can come from tmux-pi's own writers, a crash mid-write, a full disk or a manual edit) or **contrived** (it needs a hostile or impossible writer). Main uses the tags to decide what a further round is worth.

Run `tests.test_wait_timeline` and `tests.test_session_env` once through `tools/agent/test-run.sh`:
- use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground.

Report the timeline module's time against round 4's.

Touch only tools/waits/status.py and tests/test_wait_timeline.py. Do not touch the default tmux server. Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
