# PD-D round 2: get_skill works in the supported Claude main (Main, 2026-11-10)

Review review-coder-5b6d0da0af-090604 (REQUEST_CHANGES), report and logs:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-5b6d0da0af-090604/`.
Keep everything else round 1 built and the review confirmed. The bar is
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md` (on main; read it there).

## F1 (realistic, Closes by: code)
`bin/tp-main` sets `TP_MODE=orchestrator`, and `claude/mcp/subagents.mjs:15` (`active`) then lists and serves no tools at all, so
`get_skill` is missing in the supported Claude main. Every plugin command that calls it fails there, not only the two new ones:
counterpoint, make-qa, product-fit, threat-model, product-design, prepare-cases (`claude/plugins/tmux-pi/commands/*.md`). This is a
production defect.
- Rule: `get_skill` (read-only, returns a catalogued skill body) is listed and served in the orchestrator main and the legacy main,
  never in a worker task (`TP_TASK_DIR` set) unless it already was. Every other tool keeps its current gate exactly: the legacy
  worker-management tools stay hidden in the orchestrator. Decide it per tool in one place, not by widening `active`.
- Test: for each environment (orchestrator main as `bin/tp-main` sets it, legacy main, worker task, outside tmux) list the tools and
  call `get_skill` for every command file found by code under `claude/plugins/tmux-pi/commands/` that names it. Expected: the body is
  returned in both mains; the tool list in each environment equals the base's plus `get_skill` where added; nothing else changes.
  Fail-before against 0bd81b8.
- Sweep: any other place that assumes the orchestrator main has the subagents tools (docs, README, command text); list each.

## F2 (Main's decision): accepted as is
The in-process recovery through the real `tp-agent` objective/index code is accepted as the demonstration; the worker trust guard
correctly stops a worker driving tmux panes. Say so in the report; no tmux demo.

## F3 and F4 (report checks)
- F3: make `evidence/attack_selection.py` print every history case: task id and the selected skills, before and after, never the
  request text (it may hold private names). Rerun and keep the log.
- F4: restate fail-before as methods and subtests (five failing methods, 12 failures with subtests). For the "87 baseline
  failures" in tests/test_tp_review.py at the lane base, keep a log that shows them, or drop the number.

## Report
The four-cell matrix as in round 1 (same modules plus anything you touch), fail-before for the F1 test, the sweep, counts.
Search with safe_grep and read with Read/safe_read; never search `claude/`, `config/`, `agent/` recursively or `bin/` as a
directory; never read `claude/mcp.json`, `.env*` or auth files. Synthetic only; nothing sent to any network address. Commit nothing.
`RESULT_STATUS: complete` as the last line, exactly. No background shells.
