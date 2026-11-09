# LS1c S1c round 5: absence claims by a closed rule, not by AST cases (Main, 2026-11-09)

Round 4 (coder-fe29b92cdf) was checkpoint-committed by Main as aeb74bb on lane/code-facts. Main previewed it merged with
main: 225 tests OK in all four cells, and the guard gave holdout 12, tune 8, real 0. The review is REQUEST_CHANGES:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-fe29b92cdf-170336/result.md
(read it, and `report.md` and `repro.py` beside it).

## Why this round changes the approach again
This is the fifth binding-ambiguity round. Two of this round's three defects come from listing AST cases:
- an ancestor package imported by `from pkg import sub`;
- a name in `MatchClass.kwd_attrs`;
- a name in a bytes literal.

Each list misses a case. The calibration chore use-lrucache needs "who does not use it", so absence lines stay. They must
come from rules that are closed by construction, not from more AST cases.

## Build
1. **Absence: "`<file>` does not use `<T>`" is stated only when every one of these holds.** Each check is a check on the file
   text or on the run root's import graph. None of them is an AST case list.
   - **Name absent from the text.** The bare name does not occur anywhere in the file's raw source text: code, strings,
     bytes, comments or docstrings. Use a plain substring test on identifier boundaries (`(?<![A-Za-z0-9_])Q(?![A-Za-z0-9_])`).
     This replaces `_spelled` for absence. Keep `_spelled` only if something else still needs it, and say what.
   - **No dynamic access.** The raw text does not contain any of these words: `getattr`, `__getattribute__`, `__import__`,
     `importlib`, `import_module`, `sys.modules`, `eval`, `exec`, `compile`, `globals`, `locals`, `vars`, `__dict__`,
     `attrgetter`, `methodcaller`, `runpy`, `builtins`, `__builtins__`, `pkgutil`, `pickle` or `marshal`. Keep it as one
     named tuple. A false hit only loses a line.
   - **No star import.**
   - **No path through the import graph.** Build the run root's import graph from every file's imports. Use full dotted
     paths, with relative imports resolved as in round 4. For `from a.b import c`, the file imports both `a.b` and `a.b.c`.
     - An import blocks the line when it is unresolvable inside the run root but could name a run-root module. That means
       a relative import, or a top-level name that equals a run-root package or module.
     - An import also blocks the line when it is equal to, an ancestor of, or a descendant of the target module.
     - So does any run-root module from which the target module can be reached in the graph (transitive). Any ancestor
       package's `__init__` counts as importing its submodules only if it imports them.
2. **F2: every use attribution checks for a star import.** Parameter attribution (`uses Q.pop on q`) and every other emitter
   of a "uses" line must check the file's star import through the same predicate. Find every emitter by name, list them in
   a sweep table, and give each one's guard.
3. **F4: inventory.** Every line lost or gained by the real-files probe against aeb74bb goes to a file under `evidence/`,
   with a one-line reason for each. Do the same for the guard splits if their counts move.
4. **Report** the guard per-split counts and the probe count before and after. Also report whether use-lrucache's reference
   absence lines (`tests/calibration/summarize/meaning/use-lrucache.md`) are still produced on its fixture. If not, give
   the rule that stops each one.

## Tests
- Every reproduction in the review's `repro.py` (F1-F3) gives no wrong line. Add the ancestor import (`from pkg import
  sub; getattr(sub.t, chr(81))()`), `case C(Q=x)`, `b"Q"`, a name only in a comment, and a transitive path (`u` imports
  `h`, `h` imports `t`).
- A table-driven test of the dynamic-access words: one row per word. Each row inserts that word into an otherwise clean
  file and expects no absence line. The table must name each word.
- A star-import file with a parameter attribution gives no "uses" line.
- The clean case still gives its absence line: a file with unrelated imports and no mention of the name.
- Fail-before for each new or changed test against aeb74bb. Put the script and log under `evidence/` in the task dir and
  name them by absolute path.

## Done means
- The seven modules of round 4 pass with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside tmux (`env -u
  TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy
  TP_TASK_ID=dummy`). Rerun them after your last edit and list each file's time.
- Include a Self-review section, the sweep table and SLOW_TESTS.
- Commit nothing.
- `RESULT_STATUS` on its own line, as the last line of your final message.
- Do not start background shells or watchers that outlive your work. A later turn overwrites the report Main reads.

No live model and no request to 127.0.0.1:1234. No special-casing of fixtures. Never read `.env*`, and never write that
string in a shell command. Never search `bin/` as a directory, or `config/`, `agent/` or `claude/` recursively.
