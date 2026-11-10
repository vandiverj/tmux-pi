# LS1 round 14: nested pairs must read in linear time (Main, 2026-11-09)

Review 13 (review-coder-b69bae86e7-032459, in this task's reviews) is REQUEST_CHANGES with F1 (defect) and F2 (report check).
Main committed your round-13 work as checkpoint 8560cf6 on this branch. Keep working on top of it.

## F1, reproduced by Main (`main-evidence/ls1probe17.py`, CPU seconds, py3.9, 1,000,000 bytes)
`text = 'line 1 of (' * k + 'x' * (1000000 - 12*k - 3) + '.py' + ')' * k`, appended to nothing, checked against use-jobqueue:

| k | round 13 | c8f96f5 | 9e2a680 |
|---|---|---|---|
| 2000 | 0.69 | 0.09 | 0.23 |
| 4000 | 1.55 | 0.10 | 0.69 |
| 8000 | 4.06 | 0.12 | 2.48 |

Round 13 is quadratic in the nesting depth for a fixed size (rule 20: linear, and below 2 s CPU). c8f96f5 was flat, but it read
these texts differently (it also reported `unparsed_reference`). The cause, per the reviewer: each reverse match calls `span()`
on its own pair, and `untrail()` and `scan.rfind()` walk the same suffix again for every nested candidate. The overlapping
candidates are thrown away only afterwards.

## Do
- Make the reader linear in the reply length for nested pairs. Precompute or reuse the boundary work, and do not fully read a
  candidate that an enclosing reference already covers. Keep every reading the same: the oracle (382,536 cases), counts per kind,
  the references 24/24 and corpora A/B/C must not change. Do not add a window and do not refuse long replies.
- Check the forward loop and `_floor` for the same pattern, nested or overlapping forward pairs included, and fix any you find
  the same way.
- Add a rule-20 test family for nesting, measured in CPU seconds with the existing `megabyte()` helper:
  - shapes: nested reverse pairs (the shape above), nested forward pairs (`(` * k + path + `:6` + `)` * k), mixed nested
    reverse and forward, and nesting in each bracket type and in backticks;
  - each at 1 MB with k = 16,000 (deepest), all under the 2 s bound;
  - **plus a linearity assertion:** for the reviewer's shape, CPU at k=16,000 is at most 1.5× the CPU at k=1,000 (same size).
    If that ratio is noisy, state the observed ratios over 5 runs on both interpreters and choose a bound with margin;
    do not loosen it to pass a quadratic reader.
- Fail-before: the new tests against the round-13 checkpoint commit 8560cf6 (a scratch copy of it), and report
  which tests fail.
- **F2:** correct the sweep-table explanations the reviewer named: row 6 (it was a heading-context fix), row 21 (the original reproducer was
  `{src/cli.py:6`), row 13 (already correct; no out-of-scope disposition) and row 32 (no universal bound claim; cite the new
  nesting family). Put the sweep table under the exact header the extractor expects: check the template in your request.md
  and use its column names.
- Report the rule-20 worst shape (py3.9 alone and with 4 copies; py3.14 alone) including the new shapes; the 5-module matrix;
  and the test-file times alone (each file under 60 s on py3.9).
- State which commands could reach a model (none is expected). List every script you wrote and ran. End with
  `RESULT_STATUS: complete` (or `blocked`).

Same touches, no live runs, same search rules (named files only, no rg/git grep over directories, never config/ or agent/ recursively).
