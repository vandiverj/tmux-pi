# LS1c S1c round 7: module names by path suffix; members only on single-binding proof (Main, 2026-11-09)

Round 6 (coder-36ec1ecbe7, two turns) was checkpoint-committed by Main as 0f530c6 on lane/code-facts. Main previewed it
merged with main: 227 tests OK in all four cells, the guard gave 12/8/0, and the probe went from 111 to 6. The review is
REQUEST_CHANGES:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-36ec1ecbe7-180359/result.md
(read it, and `report.md`, `repro.py` and `repro.log` beside it).

## Why
Both defects come from import resolution guessing where it should prove:
- **F1.** A member import counts as resolved when the module has any write to the name. That includes an annotation only, a
  `del`, a conditional write, or a write to an unrelated object.
- **F2.** Module names assume one `sys.path` root, so the ancestors of a namespace package under `src/` are missed.

Two rules close them. Don't add a case list.

## Build
1. **A module has every dotted suffix of its path as a name.** Any directory may be on `sys.path`, so
   `src/h/t.py` is named `src.h.t`, `h.t` and `t`.
   - For the ancestor, descendant and equality tests, and for "names a run-root module", compare an import against every
     name of every run-root module and package directory. A directory counts as a package whether or not it has an
     `__init__.py`.
   - An import whose dotted name, or any prefix of it, matches a run-root name but does not resolve to a run-root module
     cannot be resolved. It blocks absence. That is round 6's rule, now applied over all names.
2. **A member is resolved only on positive proof.**
   - `from h import x`, where `h` resolves and `h.x` is not a module, counts as resolved only if `x` passes round 3's
     single-binding predicate at `h`'s module scope: one plain unconditional `def`, `class` or `NAME = <literal>`, with
     nothing else able to change it.
   - Otherwise the import cannot be resolved, and it blocks absence.
   - Reuse the existing predicate; do not write a second one.
3. **F3.** Bring the sweep descriptions up to date, and add the per-cell runs the review names to SLOW_TESTS.
4. **Report** the guard per-split counts and the probe count before and after (against 0f530c6), and add every lost or
   gained line to the inventory. Say whether use-lrucache's reference absence lines are still produced. If one is lost,
   stop and report it; do not loosen the rules to get it back.

## Tests
- In the existing import-graph table:
  - every reproduction in the review's `repro.py`: an annotation-only, deleted, conditional or unrelated-object-write
    member; a namespace-package `h.missing` under `src/`; and target `src/h/t.py` with `import h`. Each gives no absence
    line.
  - keep-the-line rows: a member defined by a single plain `def`, and a resolvable `import h.real` under `src/`.
- Fail-before against 0f530c6. Put the script and log under `evidence/` in the task dir and name them by absolute path.

## Done means
- The seven modules of round 6, with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, outside tmux (`env -u TMUX -u
  TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
  Run them after your last edit and list each file's time.
- Self-review, sweep table and SLOW_TESTS.
- Commit nothing.
- `RESULT_STATUS` on its own line, as the last line of your final message.
- Do not start background shells or watchers that outlive your work.

No live model and no request to 127.0.0.1:1234. No special-casing of fixtures. Never read `.env*`, and never write that
string in a shell command. Never search `bin/` as a directory, or `config/`, `agent/` or `claude/` recursively.
