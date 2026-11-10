# LS1c S1c round 8: an attribute write anywhere unproves a module member; the member rule is existence (Main, 2026-11-09)

Rounds 7 and 7b (coder-747cbc5626, two turns) were checkpoint-committed by Main as 17e2661 on lane/code-facts. Main previewed
it merged with main: 227 tests OK in all four cells, guard 12/8/0, probe 6. The review is REQUEST_CHANGES:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-747cbc5626-183706/result.md
(read it, and `report.md`, `repro.py`, `repro.log` and `inventory.log` beside it).

## F1: an attribute write through an alias (code)
`import pkg.sub.t as t; a = t; a.Q = other; t.Q()` is attributed as "creates a Q". The module attribute was replaced through
another name, and the attribution scan checks only the binding of `t`.

**Rule, not an alias analysis:** an attribution through a module attribute (`m.Q`, where `Q` is the target) holds only if no
file of the run root writes or deletes an attribute named `Q` in any form (`x.Q = v`, `x.Q += v`, `del x.Q`, any root `x`).
- `_binding_index` already records these as `write` events with role `attr`. Use them. `_written_names` is the one reader of
  "writes an attribute named N"; reuse it rather than adding another scan.
- A write blocks the attribution facts that go through `m.Q` (the import-reference, call and creation lines). The import line
  itself is still a fact.
- A direct `from pkg.sub.t import Q` binding is already covered by `_single_binding`; say in the report whether a run-root
  `t.Q = v` in another file needs the same block for it, and apply it if so.
- Sweep: list every emitter that attributes through a module attribute and say which of them the rule now covers.

## F2: what a member proof must show (requirement correction by Main)
Round 7's brief said a member is proven by "one plain unconditional `def`, `class` or `NAME = <literal>`" and also "reuse the
existing predicate". These conflict: the predicate accepts any `NAME = value` and an import alias. Main resolves it in favour
of the predicate, for this reason:
- The question at the member is only whether `from h import x` takes an attribute of `h` or falls back to importing a
  submodule `h.x` that may come from outside the run root. Python takes the attribute when `h` binds `x` at module scope.
- One unconditional binding in `h`'s module scope, of any value, proves that. If the right-hand side raises, `h` fails to
  import and so does the importer.
- What `x` holds is `h`'s business. `h` is in the import graph, so every module `h` imports is walked, and every module the
  walk visits gets the `_opaque` check. A value that reaches the target makes a path. The member proof does not judge the
  value.

**Build for F2:** no code change. Instead:
- Correct the `_single_names` docstring and the report so they state this rule and the reason in one place.
- Add rows to the import-graph table that pin it down:
  - `x = unknown()` where `unknown` is defined in `h`, and `import os as x`: the line is kept.
  - `from t import Q as x` in `h` (h reaches the target): blocked by the path.
  - `x = sys.modules["t"].Q` in `h`: blocked by `_opaque`.
  - `x = nonexistent`: the line is kept (importing `h` raises).

## F3
No action. Do not change the namespace-directory collision policy.

## F4 (check)
Correct the historical sweep rows the review names, using the review report's exact corrections. In SLOW_TESTS, list each
qualifying per-cell run on its own row with its time, from the existing logs. Do not use ranges. No rerun is needed for this.

## Tests
- In the existing tables:
  - F1: the review's reproduction (alias write then call), `x.Q = v` in another run-root file, `del a.Q`, and `a.Q += 1`.
    Each gives no call or creation line for `Q`.
  - Keep-the-line row: an unrelated `a.R = v` does not block `t.Q()`.
- Fail-before against 17e2661. Put the script and log under `evidence/` in the task dir and name them by absolute path.

## Report
Give the guard per-split counts and the probe count before and after, against 17e2661. List every lost or gained line.
State whether use-lrucache's reference lines (`worker.py`, `cli.py`, `jobq.py`) are all still produced. If one is lost, stop
and report it; do not loosen the rules.

## Done means
- The seven modules of round 6, with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, outside tmux (`env -u TMUX -u
  TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
  Run them after your last edit and list each file's time, one row per cell.
- Self-review, sweep table, SLOW_TESTS.
- Commit nothing.
- `RESULT_STATUS` on its own line, as the last line of your final message.
- Do not start background shells or watchers that outlive your work.

No live model and no request to 127.0.0.1:1234. No special-casing of fixtures. Never read `.env*`, and never write that
string in a shell command. Never search `bin/` as a directory, or `config/`, `agent/` or `claude/` recursively.
