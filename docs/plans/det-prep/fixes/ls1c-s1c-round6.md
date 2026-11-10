# LS1c S1c round 6: an unresolved import under a run-root package blocks absence (Main, 2026-11-09)

Round 5 (coder-6e4bc9a105) was checkpoint-committed by Main as d90536c on lane/code-facts. Main previewed it merged with
main: 227 tests OK in all four cells, the guard gave 12/8/0, and the probe went from 319 to 111. The review is
REQUEST_CHANGES with one code finding and one reporting note:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-6e4bc9a105-173243/result.md
(read it, and `report.md`, `repro.py` and `repro.log` beside it).

## Fix
1. **F1.**
   - The case: `lib/summarize_structured.py:763` treats a known ancestor package as resolving a missing module. For
     example, `import h.missing` with a run-root `h/` makes the line `u.py does not use Q`.
   - The rule: an import whose full dotted name, or any prefix of it, is a run-root package or module, but which does not
     itself resolve to a run-root module, cannot be resolved. It blocks absence. That covers direct, relative and
     `from h import missing` forms, and transitive hops through another file.
   - State the rule once, in the function that resolves imports, and have every caller use it.
2. **F2.** Correct the two sweep rows the review names, using the review report's mappings.
3. **Report** the guard per-split counts and the probe count, before and after (against d90536c), and add any lost line to
   the inventory.

## Tests
- In the existing import-graph table:
  - add the review's direct, relative and transitive reproductions, and `from h import missing`, each giving no absence
    line;
  - keep a row where `import h.real` resolves and the file stays clean, which keeps its line.
- Fail-before against d90536c. Put the script and log under `evidence/` in the task dir and name them by absolute path.

## Done means
- The seven modules of round 5, with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, outside tmux (`env -u TMUX -u
  TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
  Run them after your last edit and list each file's time.
- Self-review, sweep table and SLOW_TESTS.
- Commit nothing.
- `RESULT_STATUS` on its own line, as the last line of your final message.
- Do not start background shells or watchers that outlive your work.

No live model and no request to 127.0.0.1:1234. No special-casing of fixtures. Never read `.env*`, and never write that
string in a shell command. Never search `bin/` as a directory, or `config/`, `agent/` or `claude/` recursively.
