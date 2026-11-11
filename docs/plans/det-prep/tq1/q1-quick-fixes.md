# TQ1 Q1: three tests that wait more than they check (Main, 2026-11-11)

Owner decision: "all as recommended" (docs/plans/det-prep/tq1/decisions.md). Reports: S1-lanes.md, S3-trial-shadow.md,
S5-review-retry.md in this directory. Kind of round: mechanical. Change tests only; no production code change. Every assertion is
kept; the rule each test checks stays checked through the real code path.

## The three fixes
1. **S1(a) `tests/test_lane_writer.py:363` `test_13_the_active_lane_limit_refuses_a_new_lane`** (~135 s under coverage). It reads
   the shipped limit (`config/orchestration.json` `lanes.max_active_lanes`, 25 now) and fills it through real dispatch. Instead:
   create `limit - 1` lanes with `tp-agent lane create` (the same `check_lane_capacity`), dispatch one lane for real, then show the
   next new lane is refused with the same assertions. It must still read the shipped limit (not a hard-coded number) and must
   still fail if `check_lane_capacity` stops counting: prove that by a scratch edit that disables the check (in your temp
   worktree, reverted) and show the test fails.
2. **S3(a) `tests/test_trial_shadow_worker_files.py:420`** `PlainFileTests.test_a_task_file_left_as_a_directory_or_a_link_...`:
   4 rows, ~8 s each. Time one row and find where the seconds go (Main's guess: the fake clock `step=10.0` means more poll rounds,
   each with a real sleep). If it is the poll loop, use the larger step or a zero real sleep in the stub, same assertions. If the
   cost is elsewhere, report what it is and change nothing.
3. **S5(b) `tests/test_retry_packet.py:433` and `:445`** (6 chain shapes, 5 label shapes x 2 retries; `plain_retry` at `:295`).
   Rows the gate lets through go on to build a packet and spawn a fake worker; the rows assert only the gate's answer (exit code and
   sentence). Time one allowed row and confirm where the time goes. If the spawn is the cost: allowed rows stop at the first step
   after the gate (e.g. a fake spawn command on the test's PATH that records it was called and exits), keeping exactly one row that
   runs the whole retry. Same assertions; the "allowed" rows still assert the gate allowed them (the stub was reached).

## Proof (in the report)
- For each test: time before and after (same machine, one run each, outside tmux), test count unchanged.
- The rule still caught: for (1) the scratch disable above; for (3) a scratch edit that makes the gate allow a refused shape
  fails the table. (2) needs only the same assertions and row count.
- Covered lines: Main measures coverage on the preview; you do not need to.

## Verification (the worker tests once)
Run tests.test_lane_writer, tests.test_trial_shadow_worker_files, tests.test_retry_packet, tests.test_session_env once with
`/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`), short fresh
`TMUX_TMPDIR` under /tmp, `perl -e 'alarm 900; exec @ARGV'`; the log starts with `date -u`, `TMUX_TMPDIR` and
`git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the full discovery. Nothing to 127.0.0.1:1234 or
any network; no Claude or pi start; private tmux socket only. Do not edit config/. Search with safe_grep, read with Read/safe_read;
never read claude/mcp.json, .env*, auth files or config/local.json. Commit nothing. No background shells.
`RESULT_STATUS: complete` as the last line.
