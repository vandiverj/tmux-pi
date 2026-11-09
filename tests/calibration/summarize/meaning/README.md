# Meaning calibration set (LS1c)

The reference for `tools/qual/summarize_meaning.py calibrate`: the 2026-11-09 hand grading of the nine tune runs
(`base`, `t1`..`t7`, `pi`) on the 12 tune chores, 12 chores x 9 replies = 108 blind grades.

- `RUBRIC.md`: the rubric the graders (Claude subagents) and the judge read, amendment 1 (absence claims) included.
- `<chore>.md`: the 12 packets, as graded. Reply ids are shuffled; seed 20261109.
- `<chore>.grade.json`: the 12 grade files (the reference).
- `key.json`: reply id -> run name, per chore. The judge never sees it. The shuffle that made it was not recorded, so
  `packet --key key.json` reuses this order; with `runs/` it rebuilds the packets byte for byte (a test checks it).
- `runs/<name>.jsonl`: the `chore` and `reply` of each run's first record per chore (from bench-results/ls1c-2026-11-09/
  and bench-results/s2-2026-11-09/tune-live.jsonl for `pi`), so the packets can be rebuilt without `bench-results/`.

Known quirk: the hand grader numbered 9 cited lines for exports-worker R7 (a markdown table with 8 cited rows); the tool
counts 8. The other 107 replies match the tool's count.
