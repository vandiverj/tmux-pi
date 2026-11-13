# HB6c round 9 (mechanical): a replayed absent task is absent, not a task to build

Review review-coder-dbb2c8b578-185827 (read its report.md, F1 and F2, and its evidence/attack.py). One low, contrived finding remains.

**F1** (`tools/waits/status.py:607–610`). The elimination replay `attempt` calls `timeline.build_task(reader, task, ...)` for a selected ID even when `timeline.load_task` returned None (because `meta.json` is hidden). That raises TypeError. `_explain` reads the TypeError as "hiding did not help", so it rejects the file that actually explains the failure. Reproduction: a selected writer with valid `meta.json` plus `queued_at: 10**400`, beside a healthy open lane. Problems list only `tasks/t-run`, with "no single file explains it". `meta.json` is missing from the list.

Fix: a replayed task that loads as None is a successful load, as `timeline.build` already treats it (`if task is None: continue`, timeline.py:907–908). Build only when the task is not None. Selected and foreign IDs must give the same attribution.

Tests: extend the existing sequential rows in `StatusTest.test_next_action_table`. Add no new method, worktree or process. Add one small matrix over selected/foreign × `created`/`queued_at` set to 10**400. Each case asserts:
- `meta.json`'s path is listed;
- the healthy lane keeps its whole `state` (`{state, since}`) and its rule.

The selected `queued_at` case must fail before the fix.

Fail-before: a `.py` in evidence/, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`, against 92c6788 extracted to /tmp. It prints **which assertions fail** on the old tree (the review's F2), not only exit codes, and exits nonzero on mismatch. Do not edit any tracked file.

report.md: name the fix with file:line, list each matrix case with its realistic/contrived tag, and give the test counts per module exactly as the records show them.

Run `tests.test_wait_timeline` and `tests.test_session_env` through `tools/agent/test-run.sh`, once on `/usr/bin/python3` and once on `/opt/homebrew/bin/python3.14`. Run them outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), with a fresh short `TMUX_TMPDIR` under /tmp, in the foreground.

Touch only tools/waits/status.py and tests/test_wait_timeline.py. Do not touch the default tmux server. Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
