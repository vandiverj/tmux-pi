# LS1c S1c round 15b: Main's decision on the lost guard lines (Main, 2026-11-10)

Answers coder-2feae8a8fc's stop (round 15, `RESULT_STATUS: blocked`): the allow-list rule loses two use-lrucache lines, three
use-jobqueue credit lines and three use-utils absence lines, all because the guard row imports a run-root module it does not supply
(`settings`, `jobq`).

## Decision: option 1, plus coverage
1. **Accept the loss.** A module the scan cannot read may run anything, including a string loader; a line that held only because an
   unseen module was assumed harmless was never proved. Keep the round-15 rule as measured: an unseen, non-stdlib module is not on
   any list and makes its importer opaque. Do not special-case unseen modules (option 3 reopens F1) and do not edit the existing guard
   rows' file lists (option 2 changes what those rows test).
2. **Expected guard counts become tune 3 / holdout 9 / real 0, real probe 0**; update the guard expectations and the docs that state
   the old counts, with each lost line listed (as your table) in the test or its comment.
3. **Add coverage that the lines come back when the code is visible:** new guard rows (or test rows) that supply all seven `src/*.py`
   files for the use-lrucache, use-jobqueue and use-utils questions, expecting every line the old rows expected. Your measurement says
   all five "does not use LRUCache" lines stay; the test must show it on both interpreters.
4. The 17 failing checks in tests/test_qual_summarize_code_facts.py: update each expectation this rule changes, and for each say in the
   report which rule makes the old expectation wrong. Do not delete a check; change its expectation or its input.

## Then finish round 15
Do everything round 15's brief lists after the stop point: the loader axis (at least 30 callables found by code), the proof row for
every list entry under the import recorder, fail-before against 493bc23, the seven modules in all four cells one after another, the
sweep, Self-review. Commit nothing. `RESULT_STATUS: complete` as the last line, exactly.
