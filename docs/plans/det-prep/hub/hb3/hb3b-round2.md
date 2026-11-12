# HB3b round 2 (lane hub-packet, on 41bc936)

Review review-coder-9c299c1e6e-115737: REQUEST_CHANGES F1-F4 (read its result.md and report.md first). Main's diagnosis: F1, F3
and F4 are code against the brief (rule 2 clips only the quote; rule 4 says a missing store is one visible line; rule 3 counts
records left out). F2 is a real input the brief did not name. The brief stands, with one Main amendment to rule 4 (below).

1. **F1, the rule text is never clipped.** A record line carries its whole rule text (whitespace collapsed to single spaces, so it
   stays one line); only the owner quote is clipped (120). Do not route the rule text through `Brief.line`'s summary budget. An
   entry that alone does not fit the section cap is left out whole and counted in the closing line. Extend the existing
   content/cap rows with the reviewer's 321-character rule (its last sentence must appear) and the 5000-character record (left out,
   counted).
2. **F2, checkout identity from git, not directory names.** Find the main checkout of a worktree through git
   (`git worktree list --porcelain`, the first `worktree` entry, read-only), so `--separate-git-dir` repositories and their linked
   worktrees resolve. Extend the existing worktree matrix with a separate-git-dir main checkout and its linked worktree.
3. **F3, a missing store is visible but calm (Main amends rule 4).** A store that does not exist gives the section heading plus one
   line: `- no work store at <path>, so no owner rules are recorded for this task` (no "ask Main": on a machine with no store that
   would be noise in every packet). An existing empty store, or one with no record in scope, still gives no section. Unusable,
   unreadable and erroring stores keep today's "could not be read ... ask Main" line. Correct the existing missing-store row.
4. **F4, count records, not lines.** The closing line's number is the records (rule ids) left out; a conflict line is not a record.
   Extend the existing capped-conflict test with the reviewer's 40 records in 20 conflicting pairs.
5. **F7, check:** for the table-driven method, state how many positive rows fail on 75ac982 individually (subtest count from a
   run against the base, evidence-file form), or qualify the claim in report.md.

Already decided (do not raise again): reviewers and other direct tp-spawn callers get no section in this lane (follow-up after
perf-review merges); `lib/launch_record.py:271` stale comment (follow-up); older tests reading the real store read-only (follow-up:
shared fixture sets `TP_WORK_DIR`).

Tests: extend the existing methods and rows; no new methods. Fail-before for each changed row against 41bc936, in the evidence-file
form (`.py` in your evidence/ dir, `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`). Run `tests.test_work_brief`,
`tests.test_lane_other_paths` and `tests.test_session_env` once through `tools/agent/test-run.sh` with a fresh `TMUX_TMPDIR`
(`--timeout` is a flag: check the run found tests). Do not touch bin/tp-review, lib/review_rounds.py or tests/test_tp_review.py
(lane perf-review). Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
