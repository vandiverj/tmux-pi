# Q4a: build the lane test setup once per class, copy it per test (Main, 2026-11-11)

Owner (2026-11-11): "do both speed up options as the highest priority". TQ1 decision (decisions.md, "all as recommended"): Q4
shared setup for S1(c) lanes, S3(b) trial/shadow full runs, S5(a) TpReviewTests. Q4a is the first slice: the shared helper and
the lane tests. Q4b (trial/shadow) and Q4c (tp_review) follow on the same helper after Q4a merges. Bar: tests earn their place;
every assertion kept.

## What is slow (S1-lanes.md, "Speed fix 2")
In tests/test_lane_steps.py, tests/test_lane_remove.py and tests/test_lane_writer.py many tests in one class build the same
repo + lane + first dispatch in setUp, 3-6 s per test. Q1 (merged a035aac) already cut test_13; do not undo it.

## The rules
1. **One helper, built once per class.** Add a small helper in tests/ (one module; extend an existing test-support module if one
   already builds these repos: find it with safe_grep) that builds a template state once per class (`setUpClass`) and gives each
   test its own copy in its own temp dir (`cp -c` / clonefile on APFS, a plain copy elsewhere; stated). A test never sees another
   test's writes. Tear-down removes every copy and the template.
2. **Copies must be real, not aliases of the template.** Anything that stores an absolute path (git worktree `.git` files and
   `worktrees/*/gitdir`, lane.json, session state, run.sh, symlinks) either is built after the copy or is rewritten for the copy
   by one stated function, and a check in the helper fails loudly if any copied file still names the template's path. Prove it:
   a test that greps every copied file for the template path finds nothing.
3. **Same tests, same assertions.** Per class, only setUp moves; test bodies and assertions stay unchanged unless a test relied on
   setUp state that the shared template cannot give (say which, and why). Test count unchanged. No deletions.
4. **Where it does not pay, leave it.** Classes whose setUp is under ~1 s or whose tests each need a different starting state stay
   as they are. Report a table: class, tests, setUp before/after, total before/after, applied or left (reason).

## Tests (tests earn their place)
The path-leak check of rule 2 is the only new test. Before/after time per module (outside tmux, each once), line coverage of
lib/lanes.py, lib/lane_commands.py and bin/tp-agent reached by the three modules before and after (coverage is installed pinned
and isolated: find how with safe_grep in docs/plans/det-prep/tq1/; do not install anything): after must not be smaller; list any
line lost.

## Verification (the worker tests once)
Run the three modules plus tests.test_session_env once before and once after, with `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp,
`perl -e 'alarm 900; exec @ARGV'` per run (a run longer than 10 min may be one harness-tracked background command, nothing left
running when you reply). Logs start with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Main
runs the four cells and the full suite. No Claude or pi start; nothing to 127.0.0.1:1234 or any network; private tmux `-L`
sockets only; never `kill-server` without `-L`. Do not edit config/ or anything outside tests/. Search with safe_grep, read with
Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. Commit nothing. `RESULT_STATUS: complete` as
the last line.
