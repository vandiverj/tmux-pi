# LS1c S1c round 10: a fact through a module path holds only if every link of the path is proved (Main, 2026-11-09)

Review review-coder-796e2f6606-202052 (REQUEST_CHANGES), F1 (family `binding-ambiguity`, repeated). Repro, run it first on both
interpreters:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-796e2f6606-202052/repro.py`
(report.md beside it has F2's corrected sweep mappings).

1. `h.py` runs `a = pkg; a.t = other`. Then `import pkg.t; pkg.t.Q()`, `import pkg.t as t; t.Q()` and `from pkg import t; t.Q()`
   all give "u creates a Q", while running them returns `'other'`.
2. `pkg/__init__.py` has `from . import other as t`. Then `from pkg import t; t.Q()` is attributed to `pkg/t.py`, but
   `t` is `pkg.other`.
Keep the line: `from pkg.t import Q; Q()` reads `sys.modules['pkg.t']` and stays a valid fact in case 1.

## Cause
Round 8 made the target's own attribute name a block (`_written_names` / `_may_write`: any write or `del` of `x.<T>` on any
object). A module-path reference `pkg.t.Q` goes through more links than `Q`: the attribute `t` of the package object, and, for
`from pkg import t` and `pkg.t` after `import pkg.t`, whatever the package's `__init__` bound to `t`. Neither link is checked.
The rule was written for one link of the chain and not asked where else it must hold, the same shape as round 9.

## Main's decision: one rule over every link
- **Rule:** a call, creation or use is attributed through a name only when every link the name is resolved through is
  proved. For a module-path name (the `modules` map in `_use_lines`: `import a.b.c`, `import a.b.c as x`, `from a.b import c`)
  the links are each attribute step after the first component (`b`, `c`, then `<T>`). A link is unproved when:
  - any run-root file writes or deletes an attribute with that link's name, on any object (extend the round-8 check from `<T>`
    to every link name; no alias analysis, same as round 8); or
  - the `__init__.py` of the package that holds the link (any ancestor on the path in the run root) binds that name by any
    statement other than an import of exactly that submodule (`from . import c`, `import a.b.c`): an `as` import of another
    module, an assignment, a def or class, a `for` / `with` target, a `global`, a star import, or a dynamic form.
  An unproved link makes that file's module-path lines undecided (the import line stays a fact, as today for an ambiguous
  binding; no "does not use" line either).
- Direct member imports (`from a.b.c import T`) read the submodule from `sys.modules`; package attribute rebinding does not
  change them. Keep them valid unless the existing round-8 rule on `<T>` blocks them. Do not suppress them for this finding.
- Build it as one function that lists a name's links and proves each one, called by every form (so a later form cannot skip
  a link). State the rule once in the `_use_lines` docstring and in `use_scan`'s.
- Do not touch `_DYNAMIC_ACCESS`, the absence-line wording, or the round-9 import-closure rule.

## Attack the rule before you report
Input space: the import form (`import a.b.c`, `import a.b.c as x`, `from a.b import c`, `from a.b.c import T`, relative
forms of each from inside the package) × the link broken (`b`, `c`, `<T>`) × how it is broken (attribute write through the
package name, through an alias of it, through `setattr` with a literal, `del`, an `__init__` binding by each statement kind
above, no break) × where (the file itself, a module it imports, a module it does not import, the package `__init__`). Print
the list by code, run each case through `use_scan` and then actually execute it, give the expected outcome and the reason,
and report mismatch and unsound counts on both interpreters. "Unsound" means a fact line that execution contradicts. Keep the
rows worth keeping in the existing table in `tests/test_qual_summarize_code_facts.py`. Fold the reviewer's repro into it.

## Report
- Fail-before against the round-9 checkpoint 1fd3693 for the new rows.
- Guard per-split counts and the probe count against 1fd3693, every lost line listed. If any use-lrucache line is lost, stop
  and report it.
- The seven modules (`tests.test_qual_summarize_code_facts tests.test_qual_summarize tests.test_qual_summarize_oracle
  tests.test_qual_summarize_structured tests.test_qual_summarize_structured_v2 tests.test_session_env
  tests.test_qual_summarize_meaning`) in all four cells after your last edit, each file's time per cell, one row per cell, and
  SLOW_TESTS with one row per cell.
- F2: correct the sweep mappings the review lists, and keep every repeated-run timing (none discarded).
- Sweep table for `binding-ambiguity` with current line numbers.
- Commit nothing. `RESULT_STATUS` as the last line. No background shells left running.
