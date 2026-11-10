# AT1: tests that need an ambient tmux server (Main, 2026-11-09)

With `TMUX_TMPDIR` pointed at an empty temp dir (the rule for every test run in this campaign), these fail in every cell
on main and on every lane, with "cannot read tmux panes":
- tests/test_review_coverage.py: 9 cases in `EveryInputEverySiteTests.test_tp_agent_collect_and_inspect` (subtests) and
  `ConsumerTests.test_tp_agent_collect_and_inspect_say_so_in_words`,
  `ConsumerTests.test_tp_agent_says_an_unusable_request_changes_is_no_rejection_and_a_new_review_may_run`;
- tests/test_lane_lease.py: `ReservationTests.test_activating_a_pane_that_is_gone_leaves_nothing_alive`;
- tests/test_lane_writer.py: `AliveOnWorkdirTests.test_alive_on_tree_is_alive_on_workdir_with_the_same_answer`.
They pass when a tmux server is running on the default socket under that `TMUX_TMPDIR`. The owner wants every failure
determined and fixed.

## Determine first
For each test, say why it reaches tmux and which of these it is, with evidence (a trace or a log):
- (a) the test depends on ambient state it should create itself: fix the test (a private `-L` socket server it starts and
  kills in setUp/tearDown, or a stub `tmux` on PATH, whichever the file already uses);
- (b) the product is wrong: `tp-agent collect`/`inspect` (or the lease/writer code) should not fail when no tmux server
  is running (no server means no live pane: the answer is "not alive", not an error). Then fix the product and keep the
  test, and add a test of the no-server case;
- (c) both.

## Rules
- Never kill or touch the default tmux server, never run `tmux kill-server` without `-L`/`-S`, never use the user's
  sockets. Every test that calls tmux sets `TMUX_TMPDIR` to a temp dir and uses a private socket.
- Do not skip, mark expected-failure or delete any test.
- Sweep (family: ambient-tmux-dependence): every test file under tests/ that calls tmux or a tp-* command that reads
  panes, run once with an empty `TMUX_TMPDIR` and no server: list each file as passes / fixed / not an instance.
- Fail-before: each changed test fails on the lane base with an empty `TMUX_TMPDIR`; keep the script and log in the task
  dir under `evidence/`, and name every log by absolute path in your report.

## Done means
1. Each of the 11 failures has its cause (a/b/c) with evidence, and is fixed.
2. The sweep table.
3. The three files plus every file you changed pass in both interpreters (`/usr/bin/python3`, `/opt/homebrew/bin/python3`)
   with an empty `TMUX_TMPDIR` and no server; each file's time listed.
4. `RESULT_STATUS` reported.

Never read `.env*`, and never write the string `.env` in a shell command, not even in an exclusion glob. Never search `bin/`
as a directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively. No live model.
