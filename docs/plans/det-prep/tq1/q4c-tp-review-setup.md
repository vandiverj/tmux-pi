# Q4c: TpReviewTests, find where the time goes and cut the setup (Main, 2026-11-11)

Owner (2026-11-11): "do both speed up options as the highest priority"; TQ1 decisions "all as recommended" (decisions.md: Q4
shared setup covers S5(a)). Q4a (merged; brief q4a-shared-setup.md, helper tests/lane_template.py) is the model. Bar: tests earn
their place; every assertion kept.

## What is slow (S5-review-retry.md, "Finding 1")
tests/test_tp_review.py: 99 tests, ~135 s per cell (Main's SR1 cells, 2026-11-11). Every test drives `bin/tp-review` as a
subprocess. `TpReviewTests` already copies lib files into a class template once (`setUpClass`, line ~33) and copytrees it per
test (`setUp`, line ~53); S5 measured 3-13 s per test, so the cost may be in the test bodies (git repos, author tasks, review
packets), not in setUp.

## The rules
1. **Measure first.** Time setUp and the body of a sample of tests (the slowest ten by name, once, outside tmux) and say where
   the seconds go: setUp copy, per-test git repo or author task building, the tp-review run itself, or waits/polls.
2. **Cut shared setup only.** What every test (or a class of tests) builds the same way moves to a class template copied per
   test with `tests/lane_template.py` (`clone_tree`/`copy_template` with its path rewrite and leak check), or replaces copytree
   with `clone_tree` where that is the cost. Do not change the helper's behaviour; Q4b may extend it in parallel, so if you need
   something new, put it in tests/test_tp_review.py and say so. A wait or poll with a real sleep that a fake clock or a smaller
   step can replace with the same assertions is in scope too (as Q1 did), named per test.
3. **Same tests, same assertions.** Test count unchanged; no deletions; a test that needs a different start is left (say why).
4. Report a table: what was changed, tests affected, time before/after; and what was left (reason).

## Tests (tests earn their place)
No new test file. Before/after time of tests.test_tp_review (outside tmux, each once). Coverage cannot see bin/tp-review (a shell
script); instead state per changed setup which assertions read the state it builds and that each still does.

## Verification (the worker tests once)
Run tests.test_tp_review and tests.test_session_env once before and once after, with `/usr/bin/python3` outside tmux (`env -u
TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp, `perl -e
'alarm 900; exec @ARGV'` per run. Logs start with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset
-q`. Main runs the four cells and the full suite. No Claude or pi start; nothing to 127.0.0.1:1234 or any network; private tmux
`-L` sockets only; never `kill-server` without `-L`. Edit only tests/test_tp_review.py. Search with safe_grep, read with
Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. If a guard hook refuses a command, stop and
report it; do not work around it. Do not approve any permission or placeholder prompt; stop and report it. Commit nothing. No
background shells. `RESULT_STATUS: complete` as the last line.
