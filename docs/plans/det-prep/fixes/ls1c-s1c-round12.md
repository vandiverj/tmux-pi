# LS1c S1c round 12: the scan assumes Python's default import state; a file that can change it is opaque (Main, 2026-11-09)

Review review-coder-3ed45a3a90-214404 (REQUEST_CHANGES), F1 and F2 (family `binding-ambiguity`, eleventh round). Repros and report:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-3ed45a3a90-214404/`.
Run them on both interpreters first. Round 11's module-identity rule (`_module_identity`) was confirmed; do not rework it.

- **F1:** `pkg/__init__.py` sets `__path__ = [__path__[0] + "/alt"]`; `from pkg.t import Q` is credited to `pkg/t.py`, Python loads
  `pkg/alt/t.py`. The graph also says "does not use Q" through the redirected helper.
- **F2:** `pkg/u.py` sets `__package__ = "alt"` before `from .t import Q`; the scan credits `pkg/t.py`, Python imports `alt.t`.

## Why the rounds keep coming
`_module_identity` models how the finder resolves a name under the *default* import state. Every round since 7 found one more way a
file changes that state (a package beside a module, an unseen `__init__`, now `__path__` and `__package__`). Closing them one at a
time does not end. This round makes the assumption explicit and checks it as a whole.

## Main's decision: one rule
- **Rule:** the scan credits through an import only when no file Python runs on the way can change the import state the scan
  models. "Import state" is one fixed tuple, with its source cited in the docstring (Python's "The import system" reference): the
  module attributes `__path__`, `__package__`, `__spec__`, `__loader__`, `__name__`, `__file__`, `__cached__`, `__builtins__`, and
  `sys.path`, `sys.meta_path`, `sys.path_hooks`, `sys.path_importer_cache`, `sys.modules`, `site`, plus the existing
  `_DYNAMIC_ACCESS` words.
- A file **changes** import state when it binds (assign, augmented assign, annotated assign, `del`, `global`, `for`/`with`/walrus
  target, import-as, function or class name) any of those module attributes, or references `__path__`, `__package__`, `__spec__`,
  `__loader__` or any `sys.` member above at all, or holds a `_DYNAMIC_ACCESS` word. Reading `__name__` and `__file__` is allowed
  (`if __name__ == "__main__"`, `os.path.dirname(__file__)`).
- **Files that must be clean:** for an import in file `U` that resolves to target module `T`: `U` itself, every package `__init__.py`
  on `U`'s own package path (it decides `__package__` resolution), and every package `__init__.py` on `T`'s dotted path. A file
  that changes import state is opaque: no creation, call, use or "does not use" line through it (as `_opaque` does today).
- One function decides this (extend `_opaque`, or one new function that `_opaque` calls); every crediting path and the graph call
  it. Nothing else checks import state.
- State the limit beside the round-9 and round-11 limits: code outside the run (site-packages, `.pth`, `sitecustomize`,
  `PYTHONPATH`, the interpreter's own startup) is outside static proof.

## Attack the rule before you report
Input space, printed by code: each import-state name in the tuple × each way to bind or reference it (the binding forms above, a
read, a mutation such as `.append`/`.insert`/item assignment, inside a function body, inside `if False:`) × location (`U`, each
`__init__` on `U`'s path, each `__init__` on `T`'s path, `T` itself, an unrelated file) × import form (`import a.b`, `as`,
`from a.b import c`, `from a.b.c import T`, relative forms) × class and function targets. For each case run `use_scan`, then
execute it (`-I`, `sys.path[0]` = the case root) and compare. Report unsound and mismatch counts on both interpreters, before
(2968ffe) and after. Fold the review's two repros in as table rows.

## Report
- Fail-before against the round-11 checkpoint 2968ffe.
- Guard per-split counts and the probe count against 2968ffe, every lost line listed. If any use-lrucache line is lost, stop and
  report it.
- The seven modules (`tests.test_qual_summarize_code_facts tests.test_qual_summarize tests.test_qual_summarize_oracle
  tests.test_qual_summarize_structured tests.test_qual_summarize_structured_v2 tests.test_session_env
  tests.test_qual_summarize_meaning`) in all four cells: /usr/bin/python3 and /opt/homebrew/bin/python3, each outside
  (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`env TMUX=/tmp/x,1,1 TP_SESSION=dummy
  TP_TASK_ID=dummy`), after your last edit, each file's time per cell. Each log starts with `date -u` and the tree hash of the
  working tree.
- F3 from the review: the Site / Disposition / Evidence sweep table for `binding-ambiguity` with current line numbers, every
  function that maps a name to a file or reads import state; SLOW_TESTS per test, not aggregated.
- Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells left running.
