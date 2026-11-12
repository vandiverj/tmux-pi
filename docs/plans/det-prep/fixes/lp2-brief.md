# LP2: correct the stale LP1 launch test after the pi file-argument change (Main, 2026-11-12)

The full suite on main + PF1c + HB3b (2026-11-12, 5546 tests) failed
`tests/test_local_provider.py` `SpawnLaunchTests.test_the_alias_is_applied_at_the_pi_launch_and_nowhere_else_in_argv` (:183).
It fails on main alone. Cause: e97296d (the work-Mac fix for long pi argv, merged by 837c31c and 05c3aa6, owner OK "review
after merge") makes tp-spawn pass pi's system prompt as a file (`--append-system-prompt <task>/system-prompt.md`), so the
argument is now a path, the same for both launches. The test still expects the aliased launch's argument to be the text plus
SOL's standing prompt. Lane lp-test, base main. Kind: mechanical (Sonnet).

Correct the existing test; do not add a method and do not delete one. It should prove what it proved before, against today's
launch form:
1. The aliased launch's `--model` is the codex alias and the plain launch's is SOL (unchanged).
2. Both launches pass `--append-system-prompt` the task's `system-prompt.md` path (each under its own root), and the aliased
   launch's file text equals the plain launch's file text + `"\n\n"` + `PROMPTS[SOL]` (use the `system` text `launch` returns).
3. Apart from the model value, the argv lists are equal (the prompt argument is equal now too, after the `<root>` rewrite;
   compare the whole list minus the model value, and say whether the old exclusion of the prompt slot is still needed).
Check the other `SpawnLaunchTests` methods and the rest of the file for the same stale assumption (a prompt passed as text);
correct any you find, and list each in report.md with the line. Read e97296d's own test (tests/test_orchestration.py) so the two
agree.

Fail-before is the current failure on main (show it once in the evidence-file form: `.py` in your evidence/ dir, run as
`cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`, or a test-run.sh record of the unchanged module). Run
`tests.test_local_provider`, `tests.test_orchestration` and `tests.test_session_env` once through `tools/agent/test-run.sh`,
`/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), fresh
short `TMUX_TMPDIR` under /tmp, foreground, one after another; the tests use a private `-L` socket, never the default tmux server.

Edit only tests/test_local_provider.py (and tests/test_orchestration.py only if it holds the same stale assumption). Do not
edit bin/, lib/, config/. Never read config/local.json (the tests use TP_LOCAL_CONFIG fixtures). Search with safe_grep, read
with Read/safe_read. No network, nothing to 127.0.0.1:1234, no Claude or pi start. Approve no prompt; a placeholder or
permission prompt: stop and report. Commit nothing. No git notes. No background shells. `RESULT_STATUS: complete` as the last
line.
