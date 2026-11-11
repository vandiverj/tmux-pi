# TQ1 S5: review and retry tests, measured (Main, 2026-11-11)

Plan: docs/plans/det-prep/tq1-test-consolidation.md. **Nothing is changed yet: each "Decide" line is the owner's.**

## What was measured
`tests.test_tp_review`, `tests.test_review_rounds`, `tests.test_retry_packet`, `tests.test_failing_tests` under coverage 7.6.1
(pinned, isolated venv outside the repo) on a clean worktree of main 468b9b0, one context per test, subprocesses included. 3 shards,
279 tests, all passed. No test calls a model.

## Where the time goes (four-cell wall per module, ~400 s per cell for the section)
tp_review 145 s (99 tests), review_rounds 101 s (120), retry_packet 90 s (35), failing_tests 68 s (25).

### Finding 1: `test_tp_review` (145 s per cell) is outside coverage's view
All 99 tests drive `bin/tp-review`, a shell script, as a subprocess, so coverage measures none of what they reach; it cannot say
which tests overlap. Each test builds its own repo, session and author task (3-13 s each). This is the same per-test setup cost as
S1 (lanes) and S3 (trial/shadow).
Proposal: add `TpReviewTests` to the shared-setup lane S1 proposed (a template repo + session built once per class, copied per
test). Overlap is judged by reading the assertions, not by coverage. **Decide:** include (recommended) / leave.

### Finding 2: the repeated-family gate table, ~106 s summed
`tests/test_retry_packet.py:433` (6 chain shapes, 75 s under coverage) and `:445` (5 label shapes, each retried twice, 31 s) run a
real `tp-agent retry` per row (`plain_retry`, `:295`). A refused retry stops before anything is spawned and is fast; a retry the
gate lets through goes on to build a packet and spawn a (fake) worker, which is where the time is. The rows check the gate's
answer (exit code and the exact sentence), not the spawned task.
Proposal: the consolidation worker times one allowed row and finds where the seconds go after the gate. If the spawn is the cost and
the row checks only the gate's answer, the allowed rows stop at the first step after the gate (e.g. a fake spawn command in the
test's PATH that records it was called), keeping one row that runs the whole retry. Same assertions. **Decide:** apply
(recommended) / leave.

### Not a problem
- `test_review_rounds` (101 s): 120 tests; the slowest, `FourStateTests` (`:588`, 34 s), is a 3 x N table over every way base, head,
  index and files can differ, which is the rule under test. Its rows are in-process and cheap; leave.
- `test_failing_tests` (68 s): 20 s of class setup, 25 tests; holds 449 lines no other module in the section reaches. Leave.

## Redundancy (candidates only)
- **Identical coverage:** 33 groups, 73 tests, mostly `test_review_rounds` table rows written as separate tests over one rule.
  Same treatment as S1: fold only rows that differ in input alone, after reading their assertions and `git log -L` for the bug each
  was added for. Small saving (they are fast); listed for the S1 fold lane if the owner wants it there.
- **Tests that alone hold coverage** (keep): `test_failing_tests.InvestigateTests.test_an_unknown_task_names_list_and_list_works`
  (19 lines), `test_review_rounds.StepBaseTests.test_a_step_that_merges_an_upstream_is_reviewed_by_its_resolution_...` (13),
  `SecretsStayOutTests.test_odd_names_are_taken_literally_...` (7).
- **Not measured:** 105 tests (the 99 of tp_review plus 6 that read source text).

**Decide:** (a) finding 1 into the shared-setup lane; (b) finding 2; (c) fold the review_rounds identical groups with S1's folds.
Main's recommendation: (a) and (b); (c) only if the S1 fold lane goes ahead.
