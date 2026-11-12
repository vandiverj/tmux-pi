# HB7a: owner decision (adopt the friction report?), lane hub-friction head 584d109

The brief says "Adoption is the owner's", so Main stopped before the merge. Nothing from this lane is in main.

## What it adds
The lane adds `python3 tools/friction/report.py --map docs/plans/det-prep/hub/hb7/frictions.json [--store DIR] [--labels FILE ...] [--memory DIR] [--json]`.
- **What it does:** it lists the process corrections the owner gave, grouped under named points ("keys"). For each point it shows whether the owner has said it, where Main proposes it should live durably, and whether it came back after the first correction. It also measures how long Main's instructions and the role files are.
- **Read-only:** it writes no prompt, role file, memory or record, and prints no quoted text. Pointers and counts only.

Rules that came out of review:
- **Only the owner's own words make a point `active`.** That means an HB3 decision record by the owner with a quote, or an H48 label row, since that set is the owner's own messages. Main's memory files are always Main's. Worker text never raises a point, whether the worker mark is on one item or on the whole point.
- **Grouping comes only from the key file**, which Main writes and the owner can correct. It never groups by similar wording, and an item with no key is listed as "ungrouped".
- **Recurrence is counted per session first:** the number of later sessions in which the point came back. The message count is shown second, because one conversation can repeat a point several times.
- **A folder it cannot read is a listed problem**, and the other sources still report. This holds on Python 3.9 and 3.14. On 3.13 and later, `glob` skips an unreadable folder silently, which round 3 found.

## What it shows on this Mac (labels A+B, Main's memory folder)
| measure | value |
|---|---|
| HB3 `correction` records | **0**: the store holds no records of any kind; Main has not been writing them |
| owner process corrections (H48 labels) | 46: 15 under 5 keys, 31 not grouped yet |
| keys `active` (owner said it) | model choice; tools are for target projects; safe search from the start; save state for restart; run only affected tests |
| keys `candidate` (Main's diagnosis) | H12 fix briefs state the rule; H22 parser briefs give the grammar first; H26 detectors fail closed from round 1 |
| came back in a later session | **save state for restart: 4 sessions**; safe search: 1; the other three: 0 (run only affected tests: 3 messages, all in one session) |
| Main's feedback memories | 13, all ungrouped (the key file names none yet) |
| contract size | main-prompt + roles 147 lines / 4403 words; with the worker contract 157 / 4715; **0 lines carry the evidence behind them** |
| run time | ~0.15 s |

## What it points to (in order)
1. **Main records the owner's corrections when they happen** (`tp-agent work decide --kind correction --by owner --quote ...`).
   - **Why:** today, the only record of a correction is a transcript label or Main's own memory. So recurrence cannot be measured from now on, and `tp-agent brief` shows no rules in force.
   - **What it takes:** a line in claude/main-prompt.md, which needs the owner's OK.
2. **Main fills in the key file:**
   - the 13 memories and the 31 ungrouped labels;
   - its own work, with no code change;
   - the owner corrects any key.
3. **"Save state for restart" is the one point that kept coming back** (4 later sessions). HB3's `tp-agent brief` and checkpoint are its proposed home. Watching this key after HB3 is the before/after test.
4. **HB7b, the prune pass:**
   - **Why:** none of the 147 contract lines says what justified it.
   - **What it does:** it gives each line a pointer, and judges a removal the same way as an addition.

## Rounds
| Round | Worker | Review | Result |
|---|---|---|---|
| 1 (Opus) | coder-044ceb809b, d47b47f | review-coder-044ceb809b-083740 | REQUEST_CHANGES: quote marks treated as owner authority (Main's brief wording); unreadable folder crashed |
| 2 (Sonnet) | coder-d9d6fa7eb1, 5217f81 | (Main's cells) | py3.14 failed: unreadable store silent through `glob` |
| 3 (Sonnet) | coder-42a68701b3, 032e048 | review-coder-42a68701b3-085324 | REQUEST_CHANGES: key-level worker mark ignored (Main's wording); store root not checked; deferral missing from --help |
| 4 (Sonnet, rule-first) | coder-b5a566b6cc, 584d109 | review-coder-b5a566b6cc-085938 | APPROVED_WITH_NOTES (the window between listing a folder and reading it stays a known limit) |

**Checks:**
- **Cells** on main 3ffa8cf + 584d109 (tree c2d663b): test_session_env, test_friction_report and test_docs_wiring, 41 tests x 4 (py3.9/3.14, outside tmux / simulated in-tmux env), 0 failures.
- **Fail-before** of round 4's tests on 032e048: 22 failures. Round 3's on 5217f81: 1 failure.
- **Private-name check:** 1 line, config/routines.json:42 (owner-kept).
- **Full suite** on the same preview: 5538 tests OK (skipped 78), 550 s (5535 before + the module's 3).

**Process notes:**
- The guard refused inline Python for the worker (round 1) and the reviewer (round 1). Main allowed the standing narrow form: a `.py` file in the worker's own evidence folder, run with `python3 -I`.
- The brief got the label files wrong: the kind and repeat flag are on every row. Round 1's worker found it.

## Options (Main recommends A)
- **A. Adopt the report:**
  - Main closes the step and merges.
  - Main then does point 2 (the key file).
  - Main asks the owner for the main-prompt line in point 1.
- **B. Adopt and do point 1 first:** same as A, but the main-prompt line comes before the key-file work.
- **C. Park it:**
  - keep the lane;
  - go straight to recording corrections (point 1);
  - measure later.
