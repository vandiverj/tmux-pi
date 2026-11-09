# RV-1 — a rewritten file is attached whole, not as a diff bigger than the file (Main, 2026-11-08)

## Failure
`tp-agent review coder-d50afe8142` (LS1 round 6) refuses: "review packet ... needs ~341496 chars (max 300000)".
The round diff (lib/review_rounds.py `prepare`, diff since round 4) is 229931 chars. Per file, diff vs the file as it is now:

| file | diff | file now |
|---|---|---|
| tests/calibration/summarize/answers.jsonl | 100864 | 43550 |
| tests/test_qual_summarize.py | 77674 | 65072 |
| tools/qual/summarize_check.py | 30005 | 15335 |
| tests/calibration/experiments.md | 15324 | 35238 |
| config/local.example.json | 3589 | 4073 |
| tools/qual/summarize_run.py | 2315 | 11956 |

A rewrite's diff repeats every old line as `-` and every new line as `+`; answers.jsonl has ~2k-char lines. The reviewer
reads more and sees less. Taking the smaller of the two per file gives 145185 chars, and the packet fits.

## Rule
For a modified text file, when its hunks are longer than the file as it is now, the section shows the file as it is now
instead of the hunks: same `diff --git` header, a one-line note naming the reason and both sizes and the scoped git command
that prints the real diff, then every line of the new file prefixed `+` (so gate() and redaction see the same line shapes as
a diff). Every other entry kind (added, deleted, mode change, binary, submodule, sensitive, generated) is unchanged.

## Done means
- The single site is `entry_patch` (or a helper it calls); both the round diff and the base diff get the rule, no second path.
- Redaction (redact_diff) still applies to whole-file sections: a test with a secret-like value in a rewritten file shows it
  replaced.
- gate() still refuses a section for an unchecked or flagged path; a whole-file section whose file contains a line starting
  `diff --git ` cannot forge a header (the `+` prefix), with a test.
- Tests: smaller-diff file keeps its hunks; larger-diff file becomes whole; equality boundary stated and tested; the note's
  command, run in the test repo, prints the real diff.
- Re-measure on the LS1 worktree (tmux-pi-wt-local-summarizer, task coder-d50afe8142, read-only: run `prepare` into a
  scratch dir and report the new diff file size; do NOT start a review).
- Sweep: every other place that attaches diffs to a review packet (bin/tp-review, lib/review_rounds.py scope, timers),
  listed with verdict.
- Matrix: test_review_rounds and test_tp_review, both Pythons x both modes.
