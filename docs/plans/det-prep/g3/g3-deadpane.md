# G3 round 8: the collect check looks for a live pane, not a kept dead one (Main, 2026-11-09)

## What happened
Live run 8 (36b870d, battle-soccer) passed setup, dispatch, prompt, decline, retry, review (the pi reviewer started; the
round-7 fix works), close and merge, then failed at `collect`:
`FAILED at step 'collect': the panes of the three tasks are gone from tmux (saw: retry: %129)`.
Run record: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/g3/` (newest `live8-*.json`); kept state
`/private/var/folders/7r/c7qj77bs2n3f24x6zkbdkc_00000gn/T/g3-11100319-201o-26add9-74e5mm00/state/tasks/`.

Cause, read by Main from that state and the pane captures:
- `bin/tp-spawn` sets `remain-on-exit on` for every worker pane, so the scrollback stays readable after the worker ends.
- `lib/worker_process.end_process` ("End the worker process ..., keeping its pane and scrollback") ended the retry's
  process at 03:20:23, when the review was dispatched (`reaped.json`: signal SIGTERM). Its pane then showed
  `Pane is dead (signal term ...)` and stayed.
- At `collect`, the retry had no live process left, so nothing changed, and `list-panes -a` still listed the dead `%129`.
- The first task was stopped (`tp-agent stop` kills its pane), so its pane was gone. The reviewer's process was ended
  by collect itself.
- The check (tools/smoke/g3_e2e.py ~759-762, tamper `pane-alive`, README "the panes of all three tasks are gone") counts
  any listed pane, dead or alive. What it was written to catch, per its tamper (a window running `sleep 300`), is a pane
  whose process is still alive.

## Main's decision
- Keep `collect`'s design: a collected task's pane may stay as a dead pane with its scrollback. Do not change
  `bin/tp-agent`, `bin/tp-spawn` or `lib/worker_process.py`.
- The check becomes: for each of the three tasks, its recorded pane is absent, or present with `#{pane_dead}` = 1. A
  present pane that is not dead fails, naming the pane and its current command. Rename the check text to match ("no task
  pane is still running"), and update the tamper table key text and the README line.
- Fake mode must reproduce this run's order: the retry's process is ended at review time and its dead pane is still
  listed at collect. Add a test that fails on 36b870d for exactly that reason and passes after the change. Keep the
  `pane-alive` tamper failing the run.
- F3 from the round-7 review: the README (~line 29) and the `require_env_flag` message say an old tmux is refused
  "before anything is made". The integration branch is already created by then. Say "before the tmux session is created".

## Attack the rule before you report
Input space: each of the three tasks × its pane (absent, dead with remain-on-exit, alive running the worker, alive running
something else, a recorded pane id reused by another session's pane) × fake mode. Print by code, run each, expected outcome
and reason, counts. Keep the worth-keeping rows as tests in tests/test_g3_e2e.py or tests/test_g3_e2e_tamper.py.

## Report
- Fail-before against 36b870d for the new tests.
- tests.test_g3_e2e tests.test_g3_e2e_tamper tests.test_session_env in all four cells (both interpreters, outside and inside
  tmux) after the last edit; time per file per cell with each log's file name, the tree hash it ran on and its start time.
- Never run the live smoke from your pane; Main runs it. Never touch the default tmux server: tests use TMUX_TMPDIR from
  `mktemp -d` and a private `-L` socket.
- Commit nothing; `git add` new files. `RESULT_STATUS: complete` as the last line.
