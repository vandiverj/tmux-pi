# Q4b/Q4c findings: test time is in tmux-pi's own commands, not in test setup (Main, 2026-11-11)

Q4a (lanes) paid off with a shared template. Q4b and Q4c measured before changing, as their briefs required, and found setup is
not the cost:

- **Q4b, trial/shadow** (coder-9ad46636ec): setUp 0.02 s, prepare 0.2 s per test. A full-run test (6.5-7 s; ~19 of them) runs six
  `tp-agent` subprocesses at ~1 s each; the review step alone is 5.8 s of 6.9 s. `bin/tp-agent --help` takes 0.06 s wall, so the
  second is spent inside the commands (work done per call), not interpreter start. Lane tq1-trial carries only the run_parallel
  unique-temp-name fix (2a2f339).
- **Q4c, tp_review** (coder-d641bfaa3a): setUp 0.81 s across 99 tests (0.6 % of 128 s). Each test is one `bin/tp-review` run: 1.47 s
  plain, ~18 `python3` processes plus `tp-agent` and `jq`; ~3.4 s with 200 files because per-file scope work grows with the file
  count. No sleeps or polls. Its one edit (clone_tree instead of copytree, 5.5 ms to 0.3 ms per test) saves ~0.5 s of 128 s:
  parked, not merged (not worth a review round).

## The lever (owner decision: product code, not tests)
Fewer processes per `tp-review` run (fold the ~18 python3 calls and three `tp-agent` calls into one python entry; batch the
per-file scope work) and find what each `tp-agent` command does in its ~1 s. This speeds the tests and every real review and
dispatch. It changes `bin/tp-review`, `bin/tp-agent`, `lib/review_rounds.py`: product code under review gates, so it is a lane of
its own (measure first: a per-call profile of one tp-review run and one tp-agent dispatch), after SR1 merges (SR1 edits
bin/tp-agent).
