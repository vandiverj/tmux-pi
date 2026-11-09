# LS1c meaning grading, 2026-11-09 (record)

The hand grading behind the LS1c meaning bar (run-log "LS1c tune runs re-scored strict vs meaning"): 12 tune chores x 9
replies, graded blind by Claude graders with RUBRIC.md (amendment 1 included). Key seed 20261109; the reply order is in key.json.

- `RUBRIC.md`, the 12 packets (`<chore>.md`), the 12 grade files (`<chore>.grade.json`) and `key.json` are byte-identical to
  the files in Main's untracked bench-results/ls1c-2026-11-09/meaning/ (checked with cmp, 26 of 26).
- `runs/<name>.jsonl` hold only `chore` and `reply` of the first record per chore of each graded run. Main checked each one
  against its source run file (base, t1-t7 under bench-results/ls1c-2026-11-09/, pi = bench-results/s2-2026-11-09/tune-live.jsonl):
  9 of 9 equal, 12 chores each.

tests/calibration/summarize/meaning/ (lane meaning-grader, MG1) holds byte-identical copies as the grader's calibration set,
declared in its frozen-copies.json against this commit.
