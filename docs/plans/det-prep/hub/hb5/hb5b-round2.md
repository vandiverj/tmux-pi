# HB5b round 2 (lane hub-main-runs, on 0fd8e0e)

Review review-coder-56c0c62fe6-163110: REQUEST_CHANGES F1-F3 (read its result.md and report.md first; the reproducers are in
its evidence). Main's diagnosis: all three are real; none is a brief fault (the brief's rule 3 says "seconds Main spent
rerunning checks a worker or reviewer had already run", which round 1 read narrowly). The rules below are now explicit.

1. **F1, Main rerun seconds (rule):** each Main run is judged on its own, against every earlier worker or reviewer run (earlier
   by end time) on the same tree under the same check identity (runner, cwd, interpreter rule from round 1). If one of those
   runs has the same test set or a superset, all of the Main run's seconds count as Main rerun. If none covers it but some
   cover part of it, count the covered part with the same estimate the existing covered-pair rule uses (`estimated` /
   `unknown` as today). Other Main runs never cover a Main run for this measure (Main repeating Main is already a plain
   repeat group). A Main run before any worker run counts 0. Extend the existing relation table with the reviewer's two
   cases (expected 9 and 4) plus: a reviewer as the earlier run; a covering run on another tree (0); with another interpreter
   (0); a partial cover.
2. **F2, PATH resolution (rule):** resolve the interpreter as the command would see it: a relative PATH entry, and an empty
   entry (meaning the current dir), are taken relative to the command's cwd (WORKTREE), never the caller's. Extend the
   resolver table with: a relative entry, an empty entry (leading, trailing, `::`), and `./` forms.
3. **F3, concurrent numbering test:** make the two Main runs in the existing test actually overlap at the claim, through a
   synchronisation both children wait on (a fifo or a file barrier in the test's temp dir), no sleeps. Show that the test
   fails if the claim is made non-atomic (for example a temporary local edit to a plain existence check, reverted; the
   evidence-file form: `.py` in your evidence/ dir, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`), and pass
   repeat-run evidence (the test 10 times) in report.md.

Fail-before for F1 and F2 on 0fd8e0e in the same evidence-file form. Update report.md's attack tables.
Run `tests.test_check_repeats`, `tests.test_agent_tools.TestRunTests` (or the class holding the touched tests) and
`tests.test_session_env` once each through `tools/agent/test-run.sh` (`/usr/bin/python3` outside tmux, fresh short
`TMUX_TMPDIR`, foreground). Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete`
last.
