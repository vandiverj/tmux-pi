# LS1c S1c round 14: one per-file test decides whether a file's imports show what it loads (Main, 2026-11-10)

Review review-coder-a18eecd8be-235555 (REQUEST_CHANGES), F1 (family `binding-ambiguity`, thirteenth round). Report, `repro.py`
and both interpreters' logs:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-a18eecd8be-235555/`.
Run `repro.py` on both interpreters first (26 false credits of 32 on each). Round 13's start set, binding enumeration, ast-field
completeness test, guard counts (12/8/0), real probe (0) and the three use-lrucache lines were confirmed; keep them.

## The defect
- **F1:** `_runs_clean` (summarize_structured.py ~1163-1171) hands `_changes_import_state` to `_walk_blocked`, not `_opaque`. A file
  with `from helperpkg import *` makes Python import every submodule `helperpkg.__all__` names (here `helperpkg.redirect`, which
  redirects `pkg.__path__`), and `_imports_of` drops the star member, so the walk never sees that module: false credit.

## Why the rounds keep coming
Round 13 removed a second binding walk. This is the same shape one level up: two per-file tests ("changes import state" and
"opaque") where the walk that grants credit uses the narrower one.

## Main's decision
1. **One per-file test, used by every traversal.** `_opaque` (star import, or changes import state, or a `_DYNAMIC_ACCESS` word) is
   THE test a visited file is held to, in the credit walk (`_runs_clean`), the absence walk (`_reaches` / `_refers`) and any other
   caller that asks whether a file's imports show what it loads. No traversal passes `_changes_import_state` (or any other narrower
   test) on its own. List every `_walk_blocked` call and every per-file predicate it is given, by code (grep the module, print them),
   and show each now uses the one test.
2. **A star import is opaque.** Do not resolve `__all__` to win credit back (round 12's rule: do not loosen to recover lines). A file
   with a star import anywhere on the walk blocks credit for U, as an unresolved import does.
3. References and bindings stay as round 13 defined them.

## Attack the rule before you report
Extend the round-13 attack with an import-form axis that includes every form that can load a module the importer's own import list
does not show: `from M import *` with M a package whose `__all__` names a submodule (list, tuple, augmented, computed), with M a plain
module, and the existing forms. × the round-13 locations (U, each initializer on U's path and on T's path, T, a helper imported only
by an initializer, a helper two deep, an unrelated file) × class/function targets, plus the absence variant. Fold the reviewer's 32
repro cases in as rows. Execute each case (`-I`, `sys.path[0]` = case root). Print the input space by code; report unsound and
mismatch counts on both interpreters at 6419dd1 (before) and after.

## Report
- Fail-before against 6419dd1.
- Guard per-split counts and the real probe count against 6419dd1, every lost line listed. If any use-lrucache line is lost, stop and
  report it.
- The seven modules in all four cells, one after another (as round 13's brief), each log starting with `date -u` and
  `git add -A && git write-tree`; each file's time per cell.
- F2 (round 13 review): SLOW_TESTS per test **per cell**, including every repeated run in tests.jsonl, not one aggregate. Correct
  sweep row 23 (144 initializer-helper cases plus 4 ExceptHandler.name cases in T's initializer) and row 35 / Self-review (`_members`
  feeds use attribution at use_scan, not version diffs).
- Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells left running.
