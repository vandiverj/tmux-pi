# LS1c MG1 round 2: close review F1-F3 (Main, 2026-11-09)

Round 1 (coder-60d1d5b33e) was reviewed REQUEST_CHANGES:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-60d1d5b33e-151446/result.md
(the report.md beside it has the reproductions). Read it first. The round 1 brief
(docs/plans/det-prep/fixes/ls1c-mg1-meaning-grader.md) still applies.

Note: Main rebuilt this lane on 898eeb2 (the record of the hand grading) and cherry-picked your work unchanged.
tests/calibration/summarize/meaning/frozen-copies.json declares the calibration files as copies of that record. Do not edit
any file it lists. If a fix seems to need that, stop and report it.

## Fix (each with a test that fails on the round 1 tree, b11907d)
- **F1:** `judge`, and any other command that collects packets from a directory, takes only packet files. RUBRIC.md,
  README.md and any other Markdown without the packet sections are not packets. Choose by shape (the `## Replies` section),
  not by name. Add a test that runs `judge` (stub `--pi-command`) on the committed calibration directory itself and checks
  that exactly the 12 chores are judged.
- **F2:** in `calibrate` (and `score`), a judge grade that lacks a fact the packet or reference has must be reported, as a
  disagreement or as an invalid grade (`judge_error`) that fails calibration. It must never be a KeyError. Pick one, say
  which in the docstring, and test it with a reference grade copy whose reply has F1 removed.
- **F3:** in the report, say exactly what the round 1 fail-before shows: one module-import error, which stops all 21 tests.
  For this round, run each new test against b11907d. Put the script and log under `evidence/` in the task dir and name them
  by absolute path.

## Done means
1. F1-F3.
2. tests.test_qual_summarize_meaning, tests.test_qual_summarize, tests.test_qual_summarize_oracle,
   tests.test_qual_summarize_structured, tests.test_qual_summarize_structured_v2 and tests.test_session_env pass with both
   `/usr/bin/python3` and `/opt/homebrew/bin/python3`, outside tmux
   (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside
   (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`). Rerun them all after the last edit and list each file's time.
3. `RESULT_STATUS` on its own line.

No live model and no request to 127.0.0.1:1234. Never read `.env*`, and never write that string in a shell command.
