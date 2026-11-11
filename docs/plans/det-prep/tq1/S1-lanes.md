# TQ1 S1: lanes tests, measured (Main, 2026-11-11)

Plan: docs/plans/det-prep/tq1-test-consolidation.md. **Nothing here is changed yet: each "Decide" line is the owner's.**

## What was measured
The 10 `tests/test_lane_*` modules plus `tests.test_session_env` were run under coverage 7.6.1 (pinned, isolated venv outside the
repo) on a clean worktree of main 468b9b0, one coverage context per test, subprocesses included. 3 shards, 13 minutes wall,
372 tests, all passed. Source coverage of the lane code: lanes.py 92%, lane_commands 94%, lane_deps 88%, lane_lease 93%,
lane_remove 88%, lane_steps 95%. The lane code is well covered; the problem is time, not gaps.

## Where the time goes
Summed test time under coverage: 1753 s. In the normal four-cell runs the lane modules take ~785 s per cell. Per module (test count,
wall time in the cell logs): writer 35 / 210 s, steps 68 / 156 s, recovery 13 / 142 s, lease 25 / 88 s, remove 61 / 74 s,
other_paths 20 / 68 s, create 45 / 28 s, deps 20 / 10 s, records 68 / 10 s, index 4 / 0 s.

Nearly all of it is per-test setup: each test in writer/steps/recovery/lease/remove builds a git repo, a session and runs real
dispatch (3-6 s each) before checking one thing.

### Speed fix 1 (largest): `test_13_the_active_lane_limit_refuses_a_new_lane`, 135 s under coverage
`tests/test_lane_writer.py:363`. It reads the shipped limit (`config/orchestration.json` `lanes.max_active_lanes`, now 25 since the
owner raised it) and creates that many lanes **through real dispatch**, then checks the 26th is refused. Its cost grows with the
owner's setting. Proposal: create `limit - 1` lanes with `tp-agent lane create` (the real command, ~0.6 s each, it goes through the
same `check_lane_capacity`), dispatch one lane for real, then check the extra dispatch is refused. Same assertions, same shipped
limit, real refusal path; expected ~20-25 s instead of ~135 s, and no longer grows with the setting. No production code change.
**Decide:** apply (recommended) / leave.

### Not a problem: `test_the_reviewer_gets_the_lane_tags`, 62 s under coverage
Alone and without coverage it takes 11 s. The 62 s was coverage plus load. No change.

### Speed fix 2: shared setup per class (bigger change, propose after fix 1)
Setup is per test, not per class. In steps/remove/writer, many tests in one class build the same repo + lane + first dispatch.
Building that once per class (`setUpClass`, a template repo copied per test with `cp -c`/clonefile) would cut most of the 3-6 s per
test. It touches every test in those classes, so it needs its own lane with before/after coverage and time proof.
**Decide:** start this lane after fix 1 (recommended) / not now.

## Redundancy (candidates only: coverage never deletes a test on its own)
- **Identical coverage:** 23 groups, 61 tests reach exactly the same source lines. Most are rows of one rule written as separate
  tests (e.g. 6 tests in `OtherLanePathsTests` that each check one path-quoting case; 7 in `LaneFactsTests` that each check one
  part of the lane section). These are table-driven candidates: one test, one setup, a list of cases. Same assertions kept.
- **Zero own lines:** per module, tests that reach no line another test does not also reach: create 35/45, deps 12/20, index 4/4,
  lease 20/25, other_paths 16/20, records 47/68, recovery 7/13, remove 48/61, steps 51/68, writer 26/35. This is **not** a delete
  list: integration tests share setup paths, so a test can add a real check (an assertion) without adding lines. Read each one's
  assertions before any merge.
- **Line-subset ("subsumed"):** 145 of 360 tests. Too noisy to act on for the same reason; used only to pick which pairs to read.
- **Tests that alone hold coverage** (keep): e.g. `test_lane_lease ... test_a_retry_goes_through_the_same_reservation` (128 lines no
  other test reaches), `test_lane_recovery ... test_a_different_live_writer_is_ended_with_the_command_the_refusal_names` (35),
  `test_lane_steps ... test_at_a_terminal_the_typed_code_decides` (16). Full list: Main's analysis files.
- **Not measured:** 12 tests have no measured lines (they read source text or run in scrubbed subprocesses); listed, not judged.

## Proposed consolidation lane (after the owner's decisions)
One lane, Sonnet worker, cross-provider review:
1. Fix 1 above.
2. Fold the identical-coverage groups whose tests differ only in input into table-driven tests (one setup each), keeping every
   assertion. Start with `OtherLanePathsTests` (6), `LaneFactsTests` (7), `CreateTests`/`CommandTests` (8), the lease stale-holder
   group (3).
3. Proof: the covered-line set of the lane code is the same or larger; each folded test's old assertions are listed beside the
   row that now carries them; test count and time before and after; the lane modules plus `tests.test_session_env` pass in four
   cells.
Past catches: before folding a group, `git log -L` on its tests names the commits that changed them for a bug; the row carrying
that case is kept and listed.

**Decide:** (a) fix 1 now; (b) the fold lane; (c) the shared-setup lane later. Main's recommendation: all three, in that order.
