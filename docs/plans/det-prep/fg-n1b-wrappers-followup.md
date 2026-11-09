# FG-N1b: what the FG-N1 split left behind (Main, 2026-11-09)

FG-N1 (merged a924a5b) moved the wrapper lists out of `lib/sensitive_read.py` into a new `lib/command_wrappers.py`, and
`sensitive_read` now imports it. Main's full suite on clean main dc4ad17 (4 cells: py3.9/py3.14 × session env set/unset)
found two things the split left behind. The failure baseline is empty, so both must be fixed.

## Found by Main (reproduced)
1. **`tests/test_tp_review.py`: 25 FAIL + 1 ERROR in every cell.** `TpReviewTests.TEMPLATE_LIBS` (line 25) copies a fixed list
   of `lib/` files into a temporary install, and `command_wrappers.py` is not on it. In that copy, `bin/tp-review` fails with
   `review_rounds: cannot load the sensitive-read rule, so the diff cannot be checked: No module named 'command_wrappers'`
   and exits 1. Main added `command_wrappers.py` to the tuple in a scratch copy, and all 96 tests passed (py3.9). FG-N1's
   post-merge check ran only the affected modules, so this was missed.
2. **`config/model-catalog.json` `protected_paths`:** `lib/sensitive_read.py`, `bin/tp-sensitive-read`, `lib/redact*.py` and the other
   guards need `min_level` mid and `min_effort` high. `lib/command_wrappers.py` now decides what counts as a sensitive read but is not
   listed, so a low-effort edit to it is not raised.

## Do
- Add `command_wrappers.py` to `TEMPLATE_LIBS`.
- **Make the gap class fail loudly, not silently.** Add a test that computes the transitive closure of `lib/` modules imported
  (with `ast`, top-level and function-level `import X` / `from X import`, resolved against `lib/*.py` and `lib/*/__init__.py`) by every
  module in `TEMPLATE_LIBS` and by every `bin/` script the tests run from the copy. Assert that the closure is inside the template;
  the failure message names the missing module and who imports it.
- **Rule-3 sweep:** find every other test that builds a partial install (a fixed list of `lib/` or `bin/` files copied into a temp
  root). Do this with `git ls-files tests` and a grep over the named files it lists. Check each list against the same closure, and
  report one table row per site: file:line, the list, what is missing (if anything), and the fix or "complete". Use one shared helper
  if two or more sites need it; do not copy the closure code.
- Add `lib/command_wrappers.py` to `protected_paths` with the same `min_level`, `min_effort`, `roles` and `why` as `lib/sensitive_read.py`.
  Add or extend a routing test (in the module that already tests `protected path lib/sensitive_read.py`) so that a task naming
  `lib/command_wrappers.py` gets the step `protected path lib/command_wrappers.py: effort high`. Read `config/model-catalog.json` as
  one named file; never search `config/` recursively.
- Check whether any test pins the number or order of `protected_paths` entries, and update it if so.

## Done means
- **Fail-before** on dc4ad17 (the lane base) for each new or changed test. In particular, the closure test must fail on the base
  and name `command_wrappers` and its importer `sensitive_read`.
- `tests/test_tp_review.py` passes alone on py3.9 and py3.14. Report its time alone; Main measured 116 s under load, which is over
  the 60 s budget. Report it; do not change it in this lane.
- The affected modules pass in the four-cell matrix: `test_tp_review`, the new test's module, `test_route_resolve`, `test_router_catalog`,
  `test_router`, `test_model_routing`, `test_wrapper_lists`, `test_wrapper_product`, `test_sensitive_read`, `test_review_rounds`, and
  any sweep site you changed.
- State which commands could reach a model (none is expected). List every script you wrote and ran, with its path in the task directory.
- End with a line that is exactly `RESULT_STATUS: complete` (or `blocked`).

## For you: how to search
- Read named files only. Use `git ls-files` and `git diff --stat`.
- No `rg` or `git grep` over directories: `.env*` files trigger permission prompts.
- Never read `config/` or `agent/` recursively.
