# LS1c S1c round 4: absence claims and relative imports under the same positive-proof rule (Main, 2026-11-09)

Rounds 3 and 3b are committed on lane/code-facts (f135205, a437447). Previewed with main, 223 tests passed in all four
cells. The review is REQUEST_CHANGES with two new defects:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-609af31b5a-164411/result.md
(read it, and `report.md` and `repro.py` beside it). The earlier round-3 findings F1-F4 are confirmed fixed.

## Do
1. **F1: relative imports.** Resolve a relative import only the way Python would: by the importer's package, with no level
   that climbs above the run root and no relative import from a top-level module. Otherwise the import resolves to nothing,
   and nothing is attributed.
2. **F2: absence claims.** "X does not use Q" (and every other absence line) is stated only on positive proof that the file
   has no path to Q:
   - no star import of any module;
   - no dynamic form (the round-3b list);
   - no import that resolves to the target or that cannot be resolved;
   - no occurrence of the name `Q` at all.
   Otherwise give no absence line. Route it through the round-3 predicate's binding index, not a new special case.
3. Report the guard and probe counts again (round 3b: holdout 12, tune 8, real 0; probe 327).

## Tests
- Add the review's two reproductions, plus rows to the binding-form tables: a star import, an unresolvable import, a dynamic
  form, and a relative import above the root. Each gives no absence line and no attribution.
- A truly unrelated file (no imports touching the target, no `Q`) still gives its "does not use" line.
- Fail-before against a437447. Put the script and log under `evidence/` in the task dir and name them by absolute path.

## Done means
- The same seven modules and four-cell matrix as round 3, rerun after your last edit, with each file's time.
- A Self-review section.
- SLOW_TESTS listing every run of a file at 10 s or more, in every cell (review F3).
- `RESULT_STATUS` on its own line, as the last line of your final message. Do not start background watchers that outlive
  your work.

No live model and no request to 127.0.0.1:1234. No special-casing of fixtures. Never read `.env*`, and never write that
string in a shell command. Never search `bin/` as a directory, or `config/`, `agent/` or `claude/` recursively.
