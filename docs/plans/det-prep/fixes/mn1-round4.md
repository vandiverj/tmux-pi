# MN1 round 4: a given TP_MAIN_ID wins when the lock times out (Main, 2026-11-09)

Review review-coder-463d1793f7-212727 (REQUEST_CHANGES), report in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-463d1793f7-212727/`.
Everything else in round 3 was confirmed; do not rework it.

## F1 and Main's decision
The round-3 brief said that when the lock cannot be taken, the launch exports "the id read without repair (if valid)", and
also that a valid given `TP_MAIN_ID` replaces the recorded one. The two lines conflict when both exist and the lock times out.
Round 3 exports the given id; the reviewer read the brief as requiring the recorded id. **Main's decision: the given id wins**,
as round 3 does. Reason: a given id is the user's explicit choice of which main this is (for example to reach workers that
already carry that id). Exporting the recorded id instead would drop that choice and break exactly the worker linkage MN1
exists to keep. Not recording it under a timeout is the stated limit: a later launch without the override exports the
recorded id, the same as if this launch had not happened.

Do:
- Keep the behaviour. State the timeout rule in the `bin/tp-main` header comment with the order: given valid id, else the
  recorded valid id, else a fresh id for this launch only; nothing is written without the lock.
- The stderr line for the given-id case must also say that the recorded id stays `<recorded>` and later launches without
  `TP_MAIN_ID` will use it (print the recorded id when it is valid).
- Tests: keep `tests/test_main_notify.py:320-330`; add the reviewer's case (file `main-kept`, given `main-given`, lock held to
  timeout: exports `main-given`, file still `main-kept`, stderr names both, and a following launch with no override exports
  `main-kept`). Fail-before against 7bba73f for the stderr assertion.
- README: one sentence on the timeout rule if the README describes the lock.

## F2
Main's reproduction logs are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/coder-463d1793f7/main-evidence/`
(repro-usr.log, repro-homebrew.log, repro-ca79210.log). Nothing for you to do.

## Report
- tests.test_main_notify tests.test_worker_env tests.test_session_env in all four cells after your last edit, with the tree
  hash and start time of each log, and `claude plugin test claude/plugins/tmux-pi`.
- Commit nothing. `RESULT_STATUS: complete` as the last line.
