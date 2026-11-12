# TL1: test panes leave HUP-proof loops running (lane test-leak)

**Problem.** On 2026-11-11, 415 orphaned `zsh -c trap '' HUP TERM; while true; do sleep 1; done` processes (ppid 1, some days old)
held 439 of the Mac's 511 ptys (`kern.tty.ptmx_max`). Full runs then failed with "fork failed: Device not configured" (combo8
test_g3_e2e_tamper; combo10 test_pane_send, test_trial_shadow_outcomes, test_lane_recovery). Source: tests/test_process_end.py
HUP_PROOF panes (`stopper()`, test_stop_ends_a_process_that_ignores_hangup, and any test whose stop fails on purpose). Also noted
in run-log "Leaked shell loops".

**Cause.** `tests/lane_fixture.py` `TmuxCase.pane()` starts the command in a tmux pane (the pane pid leads its own process
group). Teardown either SIGKILLs the private server (`stop_server`) or kills windows (`clear_server`); both only send SIGHUP to the
pane, which a HUP-ignoring command survives.

**Change.**
- In `TmuxCase.pane()`, register a cleanup that ends the pane's process group with SIGKILL after the test, only when the pid
  still leads its own group (`os.getpgid(pid) == pid`); ProcessLookupError and PermissionError are ignored. Cleanups run LIFO,
  so make sure this runs whatever the server teardown does (order it so the group is killed even if the server is already gone).
- Check every other place in tests/ that starts a HUP-ignoring or long-lived command outside `pane()` (grep for `trap ''`,
  `HUP_PROOF`, `start_new_session`, `new-window`, `split-window`, `respawn-pane`); list each with a disposition (covered by the
  pane cleanup / own cleanup already / fixed here).
- No product code change. No test deleted or merged.

**Attack table** (one table, in report.md): input space = pane command {plain sleep, HUP-ignoring loop, HUP+TERM-ignoring loop,
command that already exited} x teardown {stop_server, clear_server (shared server), test killed the pane itself, test's own
stop already ended the group} x pid state {alive, gone, reused by an unrelated process in another group}. For each row: leaked
processes after teardown (must be 0) and no signal sent to a process outside the pane's group. Prove fail-before on main
(count the leaked loops after running tests.test_process_end alone, before and after, by exact command line and ppid 1).

**Verify once.** `tests.test_process_end`, every other module that uses TmuxCase (`grep -l TmuxCase tests/*.py`), and
`tests.test_session_env`, through tools/agent/test-run.sh with a fresh short `TMUX_TMPDIR=$(mktemp -d /tmp/tl.XXXX)`. Never
touch the default tmux server; never kill-server without -L. After the run, count leftover HUP-proof loops with ppid 1 (must be
0 from this run). Report the timing of each module.

Report in report.md. Do not commit. Do not add git notes.
