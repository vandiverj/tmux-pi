# LS1c S1c round 9: every per-file test holds over the whole import closure (Main, 2026-11-09)

Review review-coder-eba72607ca-193551 (REQUEST_CHANGES), F1: `h.py` loads Q with `pydoc.locate('pkg.sub.t.' + chr(81))`;
`u.py` imports h and calls `h.result()`. The scan says "u.py does not use Q", and running it creates Q. Repro:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-eba72607ca-193551/repro.py`.

## Cause
`_no_path_to` makes four tests on the file itself: the bare name in the raw text, a string reference (`_string_mentions`),
`_opaque` (a `_DYNAMIC_ACCESS` word or a star import), and the import path (`_reaches`). `_reaches` walks every run-root module
the file loads, but on each visited module it repeats only `_opaque` and the import tests. The name-in-text and string tests
stop at the file's edge. Round 8b added a per-file test without asking where else it must hold.

## Main's decision: one rule, applied to every module the walk visits
- **Rule:** a module the file can load is part of what the file runs. Every test `_no_path_to` makes on the file's own text and
  tree is made, with the same target tokens, on every module `_reaches` visits. Build it so a future per-file test cannot be
  added to one place and not the other (one function of the per-file tests, called for the file and for each visited module).
- Do not add `pydoc` or any loader to `_DYNAMIC_ACCESS`. Do not reword the absence line.
- **The limit, widened and stated once** in the `_no_path_to` docstring and the sweep notes: a name that reaches the code from
  outside its source, or that the source computes without writing any token of it (for example every character from `chr`),
  is beyond static proof. A finding that needs such a name is closed by this limit; a finding where a token of the target is
  written anywhere in the closure is not.

## Attack the rule before you report
Input space: the per-file tests (4) × where the trigger sits (the file; a module it imports directly; one two imports away; an
ancestor package `__init__`; a module reached only through a `from pkg import sub` member import; a module in an import cycle)
× the trigger's form (bare name, each `_string_text` form, a `_DYNAMIC_ACCESS` word, a star import). Print the list by code,
run each case, give expected outcome and reason, and report counts on both interpreters. Add the keep-the-line rows: an
unrelated module in the closure with no trigger; a trigger in a module the file does not import. Rows worth keeping go into
the existing import-graph table.

## Report
- Fail-before against ee083a4 (save `git diff` as `evidence/round8b.diff` first).
- Guard per-split counts and the probe count against ee083a4, every lost line listed. If any use-lrucache line is lost, stop
  and report it.
- The seven modules (`tests.test_qual_summarize_code_facts tests.test_qual_summarize tests.test_qual_summarize_oracle
  tests.test_qual_summarize_structured tests.test_qual_summarize_structured_v2 tests.test_session_env
  tests.test_qual_summarize_meaning`) in all four cells after your last edit, each file's time per cell, one row per cell, and
  SLOW_TESTS with one row per cell (review F2).
- Commit nothing. `RESULT_STATUS` as the last line. No background shells left running.
