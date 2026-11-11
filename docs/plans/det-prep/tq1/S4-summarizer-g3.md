# TQ1 S4: summarizer and G3 tests, measured (Main, 2026-11-11)

Plan: docs/plans/det-prep/tq1-test-consolidation.md. **Nothing is changed yet: each "Decide" line is the owner's.**

## What was measured
The 10 `tests/test_qual_summarize*` modules and the 2 `tests/test_g3_e2e*` modules under coverage 7.6.1 (pinned, isolated venv outside
the repo) on a clean worktree of main 468b9b0, one context per test, subprocesses included. 3 shards, about 6 minutes wall, 313
tests, all passed. No test calls a model: the summarizer tests use stub answers, and G3 runs in `--fake` mode (the only LM Studio
address in the section is a string compared in `tests/test_qual_summarize_structured.py:295`).

## Where the time goes (four-cell wall per module, ~430 s per cell for the section)
g3_e2e_tamper 111 s (11 tests), g3_e2e 91 s (64), qual_summarize 68 s (92), oracle 35 s (14), linear 29 s (10), the three
oracle_characters modules 72 s together (9), code_facts 16 s (48), the rest under 10 s.

### G3 (200 s per cell): the cost is shared fake runs, not per-test waste
Both G3 modules start their full fake runs once per module, in parallel (`tests/g3_e2e_support.py:150`, `Parallel`), and every
test reads the stored result. The 181 s and 147 s "slowest tests" are simply the first test in each module that triggers the shared
runs. `test_g3_e2e_tamper` makes 61 fake runs (one per tamper in `tools/smoke/g3_e2e.py` `TAMPERS`, 16 at a time); `test_g3_e2e`
makes about 18 (the flow, one injected failure per step, the config cases). The support code says one fake run takes ~11 s, "almost
all of it the real tools". This is the right shape: each tamper is a separate check that G3's assertions catch a specific break, and
coverage cannot fold them (each run reaches the same lines and asserts a different refusal).
Proposal: measure where the ~11 s of one fake run goes (tool start-up, waits, git) before deciding anything; if the waits in fake
mode are fixed sleeps, shorten them there. **Decide:** measure first (recommended) / leave.

### Summarizer oracles and linear-time tests (~136 s per cell)
- The three `test_qual_summarize_oracle_characters_*` modules (9 tests) are one generated path oracle per delimiter family, split
  into three files only so they run in parallel (commit 9889bd4). Every source line they reach, the base oracle module also
  reaches; their value is in the assertions (each delimiter is a different input the checker must handle), so they are not
  copies. The time is the size of the generated case list per delimiter (15-70 s each under coverage).
- `test_qual_summarize_linear` (10 tests) feeds megabyte inputs to show rule 20's reader stays linear. Since eb8661a the tests are
  bounded by a step count, not wall time, so the megabyte size may no longer be needed: a step-count bound shows linear growth at
  a smaller size just as well, if the old quadratic code still fails it.
Proposal: one Sonnet lane checks, for each of these two groups, the smallest input that still fails on the bug the test was written
for (git log of the test names the commit; restore that code in a scratch tree, show the test fails at the smaller size), and
shrinks to it. Same assertions; covered lines the same. Expected saving: perhaps half of the ~136 s. **Decide:** apply
(recommended) / leave.

## Redundancy (candidates only)
- **Identical coverage:** 9 groups, 25 tests (mostly G3 tests reading the same shared runs). Not folds: each asserts a different part
  of the summary.
- **Zero own lines:** 217 of 313. In `test_qual_summarize` 78 of 92; these are mostly rows of one checker with different inputs.
  Not a delete list.
- **Tests that alone hold coverage** (keep): `test_g3_e2e.HookTriggerTest.test_trust_guard_lets_the_trigger_through` (299 lines
  no other test reaches), the tamper collision test (102), `HookTriggerTest.test_the_trigger_asks_in_every_mode_then_is_denied` (43).
- **Not measured:** 39 tests (most of G3 runs its script as a subprocess that writes JSON; coverage sees the script but the test only
  reads the result).

## Proposed changes (after the owner's decisions)
(a) measure one G3 fake run's time; (b) the oracle/linear shrink lane, with the fail-on-the-old-bug proof per test. Proof as in S1:
covered lines of tools/qual/* and tools/smoke/g3_e2e.py the same or larger; time and count before and after; the section's modules
plus `tests.test_session_env` pass in four cells.

**Decide:** (a); (b). Main's recommendation: both.
