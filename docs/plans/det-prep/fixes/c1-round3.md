# Fix brief: c1 round 3 (last fix round)

The round-2 review still asks for changes. Findings: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-bba83ee1d7-155822/result.md` and `report.md` beside it. Read both.

1. Fix every `Closes by: code` finding with the smallest change that closes it, each with a test that fails without
   the fix. Do not refactor or widen scope.
2. `Closes by: none` notes: fix only if it is a few lines of tests or docs; else say why not.
3. Run the tests with output saved: `python3 -m unittest <modules> > $TP_TASK_DIR/tests.log 2>&1`, one run per
   module so each has its own count and seconds. Your report gives, per module: exact command, count, seconds, log
   path. This closes the recurring "owed: exact commands" check.
4. Report each finding id with file:line of the fix and the test name. Do not commit.
5. F1: screen --lane-facts-file with the same check --context uses (reuse it, do not copy it). F3: add the byte-identical non-lane packet comparison (render before/after with the lane code path off) since it has now been raised twice.
