# HB7b: owner decision (adopt the contract ledger check?), lane hub-prune head 567c0b7

The brief says "Adoption is the owner's", so Main stopped before the merge. Nothing from this lane is in main.

## What it adds
`python3 tools/friction/report.py --map ... --ledger FILE` and `--ledger-skeleton` (read-only; the skeleton prints to stdout).
- **The ledger** (`contract-ledger.json`, beside the contract, not inside it) gives each line of Main's prompt, the role files and
  the worker contract its reasons: pointers to the owner's words (label rows, HB3 records), a friction key, a memory, a doc heading.
  Matching is by a hash of the line, so an edited line shows up as `stale` until its entry is updated.
- **Every pointer is checked** to exist. A commit alone says when a line came in, not why, so it counts as "provenance only".
  Worker text never justifies a line.
- **Removal is judged like addition:** a line proposed for removal needs its own resolved reason; the report lists, never edits.
- Without `--ledger` the output is unchanged from HB7a.

## What it shows on this Mac
| measure | value |
|---|---|
| lines that need a reason (non-blank, not headings) | 97: main-prompt 23, coder 20, code-reviewer 18, tester 13, worker contract 10, other roles 13 |
| with a written reason today | **0**; all 97 are provenance only (one blamed commit each) |
| where they came from | 19 commits. 38 lines from the owner's test rules (tf-text, 2026-11-01); 17 from the HB2a worker contract; 14 from the first Claude-as-main commit (2026-10-01), the oldest and the first to check |
| skeleton time | ~0.14 s |

## Rounds
| Round | Worker | Review | Result |
|---|---|---|---|
| 1 (Opus) | coder-5108f0e785, f273a24 | review-coder-5108f0e785-094722 | REQUEST_CHANGES: faulty duplicates still credited; problem totals 0 while problems listed; stale entries' pointers unchecked (all code against stated rules) |
| 2 (Sonnet) | coder-1653d423f5, 567c0b7 | review-coder-1653d423f5-100210 | APPROVED_WITH_NOTES (no defect; slow-check wording, unverified history) |

**Checks:** cells on main 78272b9 + 567c0b7: test_session_env, test_friction_report, test_docs_wiring, 42 tests x 4, 0 failures.
Fail-before: round 1's tests error on main's report.py; round 2's extended rows fail 19 against f273a24. Private-name check: 1
line, config/routines.json:42 (owner-kept). Real data round trip: 97 provenance only, 0 problems.

## Options (Main recommends A)
- **A. Adopt:** close and merge; Main writes `contract-ledger.json` from the skeleton and fills each line's reason (owner label rows,
  friction keys, the plan docs behind each commit). Lines with no reason found are brought to the owner as removal candidates,
  oldest first. No contract line changes without the owner's OK.
- **B. Adopt, ledger for Main's prompt only first** (23 lines), roles after.
- **C. Park:** keep the lane; the skeleton numbers above stand as the measure.
