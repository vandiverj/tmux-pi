# TQ1: test review, consolidation and re-focus (Main, 2026-11-11)

## Owner request
"for the ritual of adding tests" … "a serious review, consolidation, clean up, re-focus of tests"; "extending or correcting tests
when possible instead of building new ones, i do not want tests simply because they were made before but becuase they properly
cover and area of the software that needs to be covered"; "we should be conservative about using frontier models in tests and it
does not look like we were". Coverage tool: "yes, install coverage pinned and isolated" (coverage 7.6.1, a venv outside the repo,
--no-deps; nothing added to the repo).

## Where things stand (measured 2026-11-11 on main)
- 195 test files / ~106k lines against 210 source files / ~109k lines; since Nov 1 tests grew +45k lines, source +24k.
- 4964 test methods; `tests/test_ctx_nav_api.py` alone is 8016 lines / 447 tests; `tests/test_g3_e2e.py` has 89 tests on one trigger;
  the sensitive-read decision is checked from at least five files.
- No test calls a frontier model. The frontier cost is in the process: Opus workers writing tests, four cells plus a full
  discovery per round, and reviewers reading large test diffs.

## Sections, not one whole run (owner 2026-11-11)
Owner: "select sections that can be done in under 20 minutes so we can interatively bring in improvements that will speed up the
other lnes while we work on consolidating and cleanup untile we have gone through everything in parts and made improvements before
doing the whole suite". So steps 1-3 run per section, slowest area first (module times from the cell logs already on disk; times
are under four-cell load):

| # | Section | Modules | Time now |
|---|---|---|---|
| S1 | lanes | test_lane_* (10 files) | ~785 s, 359 tests |
| S2 | sensitive reads and refusals | test_refusal_remedies, test_sensitive_*, test_trust_guard, other sensitive-read checks | ~485 s |
| S3 | trial/shadow | test_trial_shadow* (16 files) | ~313 s |
| S4 | summarizer and G3 | test_qual_*, test_g3_e2e* | ~433 s |
| S5 | review and retry | test_tp_review, test_review_rounds, test_retry_packet, test_failing_tests | ~400 s |
| S6 | process, status, orchestration | test_process_end, test_status_repaint, test_orchestration*, test_agent_tools, test_pane_send, test_dispatcher_runtime | ~520 s |
| S7 | ctx navigation | test_ctx_* (7 files, 809 tests) | ~127 s |
| S8 | the rest | everything else | ~800 s |

Each section's measurement runs under 20 minutes; its report goes to the owner; its consolidation lane is verified on that
section's modules plus `tests.test_session_env`. The whole suite runs once at the end.

## Step 1: inventory (Main, no repo change)
Run the section's test modules under coverage on a clean main worktree, one coverage context per test (python subprocesses included, through
a `sitecustomize` hook outside the repo). Record per test: lines covered in `lib/`, `bin/`, `tools/`, and time. Known limit: a
subprocess started with a scrubbed environment, or by a non-3.9 python, is not measured; such tests show low coverage and are listed,
not judged.

## Step 2: redundancy report (for the owner)
Per source area (module or bin script), from the inventory:
- **Subsumed tests:** a test whose covered lines are a subset of another test's in the same area and which asserts nothing the
  other does not (the assertion check is read by hand or by a reviewer; coverage alone never deletes a test).
- **Clusters:** groups of tests with near-identical coverage (attack-table rows written one test per row) → one table-driven test.
- **Uncovered source:** lines and branches no test reaches, ranked by how often the area changed or broke (git log, past review
  findings). These are where tests are missing.
- **Slow tests:** time per covered line; tests that start tmux or a full launch to check something a unit could.
- **Past catches:** for every candidate merge or delete, the failures it caught before (commit history, review rounds). A test that
  caught a real regression is kept or folded into the test that replaces it.
Output: `docs/plans/det-prep/tq1/report.md` with a keep / merge / delete / add recommendation per area. **No deletion or merge
before the owner decides per area.**

## Step 3: consolidation lanes (after the owner's decisions)
One lane per area group, Sonnet workers (mechanical), cross-provider review. Proof per lane: the covered-line set of the area is the
same or larger after the change; every past catch listed in step 2 still fails on its old bug (re-applied as a patch, then reverted);
test count and time before and after.

## Step 4: the rule going forward
Already in briefs and worker messages since 2026-11-11, and to be written into `agent/roles/coder.md` and the review role in a lane:
- extend or correct an existing test first; a new test file needs a stated reason;
- an attack table is a report artifact, not one test per row; rows become one table-driven test where they share a check;
- the report says added vs extended tests;
- workers run the affected tests once; Main runs the cells and the full suite on the preview;
- no test sends anything to a frontier model; local-model tests are opt-in (proposal awaiting the owner's number).
