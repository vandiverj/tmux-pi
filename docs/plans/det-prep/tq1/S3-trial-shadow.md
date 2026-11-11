# TQ1 S3: trial/shadow tests, measured (Main, 2026-11-11)

Plan: docs/plans/det-prep/tq1-test-consolidation.md. **Nothing is changed yet: each "Decide" line is the owner's.**

## What was measured
The 12 `tests/test_trial_shadow*` modules under coverage 7.6.1 (pinned, isolated venv outside the repo) on a clean worktree of main
468b9b0, one context per test, subprocesses included. 3 shards, 4 minutes wall, 258 tests, all passed. No test sends anything to a
model or to LM Studio: the qwen and reviewer arms are stubs. `ExplicitQwenRequestTests` (`tests/test_trial_shadow.py:492`) starts
real pi against a stub HTTP server on a free loopback port, so its requests never reach 127.0.0.1:1234 or leave the machine.

## Where the time goes (four-cell wall per module, ~300 s per cell for the section)
outcomes 63 s (22 tests), worker_files 62 s (14), run 42 s (24), branches 31 s (38), owner 27 s (56), trial_shadow 25 s (41),
records 22 s (9), gate 20 s (30), end_path 12 s (9), the rest about 1 s.

The section is much cheaper than lanes (S1, ~785 s per cell). Two places hold most of it:

### Finding 1: two worker_files tests take 33 s each
- `RunFilesTests.test_a_run_whose_files_are_replaced_while_it_runs_...` (`tests/test_trial_shadow_worker_files.py:233`): a 4 x 4
  table (run.lock, final.patch, request.md, owner record x fifo, symlink, huge, directory), each row a whole stubbed run, ~2 s a
  row. Each file is read by different code, so the rows are not copies; the table is the right shape.
- `PlainFileTests.test_a_task_file_left_as_a_directory_or_a_link_...` (`:420`): only 4 rows, yet ~8 s a row. The rows use a fake
  clock with `step=10.0` (the first test uses `step=100.0`); the likely cost is more poll rounds with real waits between them.
  Not yet proved.

Proposal: the consolidation worker times one row of `:420` with each clock step, finds where the 8 s goes (poll count x real
sleep), and, if it is the poll loop, uses the larger step or a zero real sleep in the stub, with the same assertions. Expected
~25 s per cell. **Decide:** include (recommended) / leave.

### Finding 2: ~19 tests each run a whole stubbed run with a review (9-10 s each, ~180 s summed)
In outcomes, run, records, branches and owner, each of these tests prepares a repo, a session and a run, runs the worker and the
stubbed reviewer, then checks one outcome (completed, review timeout, record failed, ...). The setup is the same; the outcome
differs by one injected fault. This is the same per-test setup cost as S1's lanes; the same fix applies (a template repo built once
per class, copied per test with clonefile). It belongs in the shared-setup lane S1 proposed, not in its own.
**Decide:** add these classes to S1's shared-setup lane (recommended) / leave.

## Redundancy (candidates only)
- **Identical coverage:** 20 groups, 51 tests. Unlike S1, these are attack rows (a fifo, a link, a directory, a renamed session, a
  forged record at the same place): the same lines are reached, but each row asserts a different refusal or survival. They are
  fast (under 1 s each). Folding them would only shorten the listing. **Leave** (no decision needed unless the owner wants it).
- **Zero own lines:** 188 of 258 tests reach no line another test does not. Same reason as above; not a delete list.
- **Tests that alone hold coverage** (keep): `test_trial_shadow.FullRunTests.test_a_full_shadow_run_changes_nothing_of_the_real_lane_and_cleans_up`
  (1009 lines no other test reaches: lanes, lane commands and steps through a real shadow run), `test_trial_shadow_end_path ...
  test_an_inconclusive_first_review_is_no_review_...` (28), `test_trial_shadow.CommandLineTests.test_f5_shadow_refuses_a_timeout_that_is_not_positive` (12).
- **Not measured:** 4 tests (they read source text or run in scrubbed subprocesses); listed, not judged.

## Proposed changes (after the owner's decisions)
Finding 1 is small and can ride with S1 fix 1 in the same Sonnet lane (same kind: a test that waits more than it checks). Finding 2
joins S1's shared-setup lane. Proof as in S1: covered lines of lib/trial_shadow.py, lib/trial_session.py, lib/trial_record.py the
same or larger; time and count before and after; the section's modules plus `tests.test_session_env` pass in four cells.

**Decide:** (a) finding 1; (b) finding 2 into the shared-setup lane. Main's recommendation: both.
