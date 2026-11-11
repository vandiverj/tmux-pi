# PD1: reading another process's output never fails on bytes that are not UTF-8 (Main, 2026-11-11)

## What happened
The HB3a round-3 full discovery (`/Users/jerrickvandiver/.claude/jobs/6061f696/tmp/hb3a3-discover.log`) failed
`tests.test_refusal_remedies.StepRemedyTests.test_a_running_process_of_an_unreadable_task_is_ended_by_the_printed_command_then_removed`
with `tp-agent: 'utf-8' codec can't decode byte 0xa0 in position 141546: invalid start byte`. Alone it passes. Main's diagnosis:
`lib/worker_process.py:100` `_commands` runs `ps -ww -ax -o pid=,stat=,command=` with `text=True` (strict UTF-8). The table holds
every process on the machine; when any process's command line holds bytes that are not UTF-8 (another test's argv during a
parallel run did), tp-agent dies. On the owner's machines the same can happen with any process, so lane remedies and process
checks would stop working for a reason unrelated to tmux-pi. Bar: realistic use.

## The rules
1. **Foreign text is read without failing.** Every `subprocess` read of output tmux-pi does not control (the process table, `ps`
   for one pid, `tmux capture-pane`, `lsof`, `git` output holding file names, and any similar read you find) decodes so that
   invalid bytes cannot raise: `errors="surrogateescape"` where the value is compared with or passed back to the OS (paths,
   command lines matched against a task dir), `errors="replace"` where it is only shown. Comparisons must still match: a task dir
   path that is valid UTF-8 is still found in a row whose other bytes are not.
2. **Sweep, do not stop at the first site.** Search (safe_grep) for `text=True`, `universal_newlines`, `.decode(` and
   `check_output(` in `lib/` and the Python scripts of `bin/`, and give a sweep table: site, what it reads, foreign or own,
   disposition (fixed / already safe / own output, left). Own JSON written by tmux-pi itself may stay strict.
3. **No behaviour change otherwise.** Same rows, same refusals when `ps` itself fails.

## Tests (tests earn their place)
Extend the existing worker_process / process tests (find them with safe_grep; e.g. tests/test_process_end.py or the module that
tests `_commands`/`task_processes`) with one table-driven test: a fake `ps` (and fake `tmux` for capture-pane if you change it)
on PATH printing rows with a 0xa0 byte, a lone 0xff and a truncated multibyte sequence beside a real task row; the task row is
still found and nothing raises. Fail-before against the lane base. Report added vs extended and the attack table (report artifact).

## Verification (the worker tests once)
Run the modules you changed tests in plus tests.test_refusal_remedies, tests.test_process_end, tests.test_lane_recovery,
tests.test_session_env once with `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u
TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 900; exec @ARGV'`; the log starts with `date -u`,
`TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the full discovery. Nothing to
127.0.0.1:1234 or any network; no Claude or pi start; private tmux socket only. Do not edit config/. Search with safe_grep, read
with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. Commit nothing. No background shells.
`RESULT_STATUS: complete` as the last line.
