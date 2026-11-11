# Q4b: trial/shadow full-run tests build their setup once per class (Main, 2026-11-11)

Owner (2026-11-11): "do both speed up options as the highest priority"; TQ1 decisions "all as recommended" (decisions.md: Q4
shared setup covers S3(b)). Q4a (merged; brief q4a-shared-setup.md, helper tests/lane_template.py) is the model. Bar: tests earn
their place; every assertion kept.

## What is slow (S3-trial-shadow.md, "Finding 2")
About 19 tests in tests/test_trial_shadow_{outcomes,run,records,branches,owner}.py each prepare a repo, a session and a run, run
the worker and the stubbed reviewer, then check one outcome (completed, review timeout, record failed, ...); 9-10 s each, ~180 s
summed per cell. The setup is the same; the outcome differs by one injected fault.

## The rules
1. **Measure first.** Time one such test's setup apart from its run (once, outside tmux). If the setup is under ~1 s of the
   test's time, it is not the cost: report where the seconds go and stop without changing that class.
2. **Use the Q4a helper.** Build the shared part once per class (`setUpClass`) and give each test its own copy with
   `tests/lane_template.py` (`copy_template`, its path rewrite and its leak check). Extend the helper only if it lacks something
   these classes need (say what and why); do not change its behaviour for test_lane_* callers. Anything a fault injection
   changes stays per test.
3. **Same tests, same assertions.** Only setup moves; test count unchanged; no deletions. If a test needs a starting state the
   template cannot give, leave it and say why.
4. **Where it does not pay, leave it.** Report a table: class, tests, setup before/after, total before/after, applied or left
   (reason).
5. **Runner race (one small fix).** tests/run_parallel.py `update_times` (around line 284) writes a fixed temp name
   `<times>.json.tmp`; two runs finishing together in one worktree fail with FileNotFoundError at `os.replace` (seen twice in
   Main's cells, test_session_env `test_every_way_of_running_the_suite_scrubs_before_any_test_code`). Give the temp file a unique
   name in the same folder (tempfile.mkstemp or pid+random) and remove it on failure; check lines 71 and 127 for the same pattern
   and fix them the same way. Prove it with the smallest check that fails before (two concurrent writers to one times file) only
   if it fits an existing test class in tests/test_run_parallel*.py or tests/test_session_env.py; otherwise state the reasoning.

## Tests (tests earn their place)
No new test file. Before/after time per module (outside tmux, each once); line coverage of lib/trial_shadow.py,
lib/trial_session.py, lib/trial_record.py reached by the five modules before and after (coverage is installed pinned and isolated:
find how with safe_grep in docs/plans/det-prep/tq1/; do not install anything): after must not be smaller; list any line lost.

## Verification (the worker tests once)
Run the five modules, tests.test_lane_steps (helper callers), tests.test_session_env and the run_parallel tests once before and
once after, with `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u
TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 900; exec @ARGV'` per run. Logs start with `date -u`,
`TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the full suite. No Claude or pi
start against a real model; nothing to 127.0.0.1:1234 or any network; private tmux `-L` sockets only; never `kill-server` without
`-L`. Edit only under tests/. Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth files or
config/local.json. If a guard hook refuses a command, stop and report it; do not work around it. Do not approve any permission or
placeholder prompt; stop and report it. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
