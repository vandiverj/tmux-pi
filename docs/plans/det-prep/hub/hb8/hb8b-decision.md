# HB8b: owner decision after round 5 (lane hub-episodes2, head cb7e57f)

Main stopped the lane after the fifth recurrence of `verification-tree-identity`. Under the away delegation Main
decided rounds 3-5 itself; closing with REQUEST_CHANGES needs the owner's `--override-review`, which Main does not use.

## What the rounds did
| Round | Worker | Review | What it fixed / what the review found next |
|---|---|---|---|
| 3 (design, Opus) | coder-15db5f08f5, 92b4c52 | review-coder-15db5f08f5-192339 | One resolver replaced the fallback branches. Next: crash without evidence, point-vs-span ordering, cross-source times, notice vs Read conflict |
| 4 (design, Opus) | coder-7536cd9385, e69d2e5 | review-coder-7536cd9385-200034 | Write-window per result version. Next: withdrawn results (new turn) still back claims; partial Reads counted as absence |
| 5 (design, Opus) | coder-da43e7e716, cb7e57f | review-coder-da43e7e716-205334 | Possible-histories model, enumeration oracle, 39,809 cases. Next: see below |

Main's diagnosis: each brief named a rule that missed one input dimension; the oracle followed the brief, so it could
not find the gap. Round 5's oracle is independent of score.py, but shares a restriction the worker added.

## Round 5 findings (all realistic)
- F1: the model requires every result version to be seen by a poll or Read; consistent histories (a withdrawal and restore
  between polls, a notice-only version) are excluded. A claim at 8.2 can become unknown because of a later notice.
  Brief-sized fix: drop the restriction in model and oracle.
- F2: safe-read output is numbered (`agent/extensions/safe.ts:101`, `claude/mcp/safe.mjs:89`); `3<TAB>RESULT_STATUS:
  complete` is not parsed, so the Read is recorded as no result. Fix: strip the line-number prefix.
- F3: safe-read caps at 2,000 lines by default without a marker; such a Read is treated as whole. Fix: a Read without an
  explicit limit is partial if it returned 2,000 lines.
- F4 (decision): the existing rule "no result in every history -> false only if the worker was busy, else unknown".
- F6 (report check), F5/F7 notes (lookalike notices; numbered `record:` lines, older code).

## Options (Main recommends A)
- **A. One more round, mechanical, on Sonnet**: F1 (drop the restriction in score.py and the oracle), F2, F3, and rule F4
  as kept. All three are named sites with exact fixes, not a new rule. If that review finds the family again, B.
- **B. Close with known limits** (owner `--override-review`): F1-F3 go to hb8b-known-limits.md. Effect: E-run scoring can
  mark a claim unknown, or backed by a numbered or capped Read, in the cases above.
- **C. Stop HB8b's claim scoring**: keep HB8b's other measures and score claims only from polls (old behaviour).

Main's cells for cb7e57f on main c42bc97: 314 tests x 4, 0 failures. Nothing from this lane is in main.

## Round 6 (owner chose A) -> stop again, owner decision (2026-11-11 22:3x CST)
Worker coder-892f06e323 (Sonnet), lane commit ef6babe; cells 314 x 4 OK; review review-coder-892f06e323-221438
REQUEST_CHANGES. Round 5's F1-F3 are fixed (both probes pass, numbered Reads and record: pointers parsed, capped Read partial).
The two new findings come only from rules the worker added **beyond the brief** (Main flagged them for the reviewer):
- F1: "result.md holds its result from its mtime on" is wrong because report.sh archives/restores with `cp -p` (old mtime kept).
- F2: "a stopped poll's whole-second stamp starts a stable window" is wrong for sub-second withdraw/restore.
Notes: F3 (E2 changed paths are 10 pass / 9 fail, not 10 each), F4 (notifier lookalikes, a known limit by the brief).

Options (Main recommends A2):
- **A2. Revert the two additions** (mechanical, Sonnet): drop the mtime window and the stable-window rule from score.py and
  the oracle; add the review's two probes as table rows (expected unknown). Effect: some claims the additions turned backed
  (the E2n-no-prompt fail run) go back to unknown, which is the honest answer. No new rule enters.
- **B. Close with known limits** (your `--override-review`): F1/F2 go to hb8b-known-limits.md.
- **C. Stop claim scoring** as before.
