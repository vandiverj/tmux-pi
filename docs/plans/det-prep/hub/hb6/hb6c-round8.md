# HB6c round 8 (design): a culprit task's failing file is found by elimination, not guessed

Review review-coder-b6a4ca2ba4-184635 (read its report.md, F1 and the sibling sites it lists). One finding remains, low and contrived.

When timeline *interpretation* raises, the fallback names only the task directory (`tools/waits/status.py:535–558, 640–641`). Interpretation means a value that parses but cannot be used, such as `created: 10**400`, a marker or prompt time, events, a transcript, a capability timestamp, a test duration or a gate timestamp. Decoding errors already name the file. Interpretation errors do not, because the reader cannot tell which value made later code raise.

Design: elimination on the failure path only.
1. When a task is a culprit and its error carries no file, run its per-task load again. Each time, hide one of the files the timeline reader read for that task, so the reader treats it as absent. Record which files the reader opened during the failed load, and hide only those.
2. Any file whose hiding lets the load succeed is listed under `problems` with its path. If no single file explains it, list the directory, as now, and say "no single file". If two files each explain it, list both.
3. This runs only for culprit tasks, so the healthy path costs nothing. State the worst-case number of reloads in report.md.
4. Do not change timeline.py. The hiding goes in status.py's reader subclass.

Tests: in-process rows in `StatusTest.test_next_action_table` only. Use the review's cases:
- foreign `meta.json` with `created: 10**400`;
- one marker or prompt timestamp case;
- one gate or test-duration case.

Each row asserts the file's path is listed, the healthy lane keeps its whole `state`, and `since` is compared, not just the keys (the review's F2 note). Run on py3.9 and py3.14.

**Write report.md.** Round 7 did not write one. It holds the sweep table (every site from the review's report with file:line, then its disposition: fixed, already correct, or unresolved), the reload bound, and the realistic/contrived tag per case.

Fail-before: a `.py` in evidence/, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`, against e8d3ab5 extracted to /tmp. Do not edit any tracked file. It exits nonzero on mismatch.

Run `tests.test_wait_timeline` and `tests.test_session_env` through `tools/agent/test-run.sh`:
- run them once on `/usr/bin/python3` and once on `/opt/homebrew/bin/python3.14`;
- run outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground.

Touch only tools/waits/status.py and tests/test_wait_timeline.py. Do not touch the default tmux server. Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
