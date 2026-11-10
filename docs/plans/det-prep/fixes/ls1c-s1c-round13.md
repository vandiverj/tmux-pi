# LS1c S1c round 13: the walk starts from every file Python runs first; one source decides what a binding is (Main, 2026-11-09)

Review review-coder-61108a8b31-224635 (REQUEST_CHANGES), F1 and F2 (family `binding-ambiguity`, twelfth round). Repros and report:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-61108a8b31-224635/`
(`repro.py`, `repro-system.log`, `repro-homebrew.log`, `report.md`). Run `repro.py` on both interpreters first. Round 12's
import-state tuple and opacity rule, the guard counts (12/8/0), the real probe (6 -> 0, lane_lease.py) and the three use-lrucache
lines were confirmed; keep them. F3 (patched real probe) is accepted as is; do not loosen the import rule to win lines back.

- **F1:** `_walk_blocked` (summarize_structured.py ~1104-1162, 1227-1228) checks the text of U's own package initializers but seeds its
  worklist only from U's imports. An initializer's imports (`app/__init__.py: import h`, where `h` redirects `pkg.__path__`, or `h`
  itself creates Q) never enter the walk: false credit, and a false "does not use".
- **F2:** `_changes_import_state` (~973-996) is its own AST walk and misses `except E as __name__` (`ast.ExceptHandler.name` is a
  string, not a Name node). `_binding_index` already records that binding.

## Why the rounds keep coming
Two hand-written lists keep falling behind Python: the list of files where the walk starts, and the list of syntax forms that bind a
name. This round replaces both with one definition each.

## Main's decisions
1. **Start set = every file Python runs before U's own code, as one list.** For a file U: U's package initializers from the outermost
   in, then U. Each one's imports (with its own path for relative resolution, and its own unresolved-import flag) seed the one
   traversal that decides credit and absence; that traversal then follows imports transitively as it does now. No crediting path, no
   absence path and no graph call may use a different start set. A test shows the start set and the seeds for a three-deep package.
2. **One source decides what binds a name.** `_changes_import_state` (and any other caller that asks "does this file bind name N")
   consumes the same binding enumeration as `_binding_index` / `_written_names`; there is no second AST walk for bindings. Make that
   enumeration complete by code: a test walks every class in `ast` (for both interpreters' `ast` modules) and lists each field that
   can bind a name (identifier fields such as `ExceptHandler.name`, `MatchAs.name`, `MatchStar.name`, `MatchMapping.rest`,
   `alias.asname`/`name`, `FunctionDef/AsyncFunctionDef/ClassDef.name`, `Global/Nonlocal.names`, `TypeAlias`, type parameters,
   plus `Name` in Store/Del context), and fails if the enumeration does not handle a listed field. Fields the enumeration decides do
   not bind must be named in an explicit exclusion list in that test with a one-line reason each.
3. References (not bindings) stay as round 12 defined them: the reference rule and the `_DYNAMIC_ACCESS` words do not change.

## Attack the rule before you report
- Extend the round-12 attack: binding forms are generated from decision 2's field list (not typed by hand) × each import-state name ×
  location (U, each initializer on U's path, each on T's path, T, a helper imported only by one of U's initializers, a helper two
  imports deep from an initializer, an unrelated file) × the existing import forms and class/function targets. Also the absence
  variant: the helper creates Q and U does not mention it. Execute each case (`-I`, `sys.path[0]` = case root) and compare.
- Print the input space by code; report unsound and mismatch counts on both interpreters, before (5922751) and after. Fold the
  review's three repros in as table rows.

## Report
- Fail-before against the round-12 checkpoint 5922751.
- Guard per-split counts and the real probe count against 5922751, every lost line listed. If any use-lrucache line is lost, stop and
  report it.
- The seven modules (`tests.test_qual_summarize_code_facts tests.test_qual_summarize tests.test_qual_summarize_oracle
  tests.test_qual_summarize_structured tests.test_qual_summarize_structured_v2 tests.test_session_env
  tests.test_qual_summarize_meaning`) in all four cells: /usr/bin/python3 and /opt/homebrew/bin/python3, each outside
  (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`env TMUX=/tmp/x,1,1 TP_SESSION=dummy
  TP_TASK_ID=dummy`), after your last edit, one cell after another. Each log starts with `date -u` and `git add -A && git write-tree`
  of the worktree; each file's time per cell.
- F4: the sweep table (Site / Disposition / Evidence) with current line numbers; correct row 19 (`_changes_import_state` is now a
  consumer, say how) and rows 20, 21 and 23. SLOW_TESTS per test.
- Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells left running.
