# LS1c S1c round 19: a module value reaches a name only through an import (Main, 2026-11-10)

Review review-coder-5633e87e87-045109 (REQUEST_CHANGES): F1 (family `binding-ambiguity`, eighteenth round), F2 note, F3-F5 report.
The report, `repro.py` and both interpreters' logs are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-5633e87e87-045109/`.
Run `repro.py` on both interpreters first and record each failing case.

Keep what the review confirmed from round 18:
- rule 1 (import cycles of any form are opaque) and rule 2 (a submodule is an attribute only when an import loads it);
- the one resolver and `_opaque` as the one per-file test; `_spellings` and the 16 allow-list entries;
- guard 9/3/0 and probe 0; the full-files and use-lrucache lines;
- every earlier attack at 0 unsound.

## The defect (F1)
`import hp; a = hp; y = a.x.v` credits Q although `hp.x` was never loaded. Rule 2 is checked where an attribute is read through
an import-bound name, but a module value that reaches another name by assignment, tuple unpacking, a container, a function's return
or any other non-import binding is still followed as the module.

## Main's decision
1. **Rule 3: a module value reaches a name only through an import statement.** A name bound by anything other than an import (any
   assignment form, augmented or annotated assignment, unpacking, `for`, `with`, walrus, a container element, a call result, a
   return value, a default argument, `global`/`nonlocal` across scopes) never resolves to a module. If its value could be a module, it
   is unknown, and any attribute read through it makes the reading file opaque. Do not track module values through assignments.
2. Put it where the resolver decides what a name refers to, once, for every binding kind. No new per-file test, no special case to win
   lines back.
3. Sweep every place a name's binding is turned into a module reference and list each with its line, showing rule 3 covers it.
4. F2 (ordered imports inside functions or classes lose lines because `loaded_before` counts only top-level imports) is a recorded
   follow-up, not part of this round.

## Attack the rule before you report
Build each input space by code, print it, and run it at d71ab79 (before) and after, on both interpreters:
- every non-import binding kind listed above × module sources (`import hp`, `import hp as h`, `import hp.x`, `from . import hp`,
  relative levels 1-3, a module imported inside a function or a conditional) × chain depth 1-3 of attribute reads × submodule loaded /
  not loaded × class and function targets × the round-17 package locations;
- round 18's cycle generator extended with relative levels 2-3 and conditional imports (F4), and its submodule attack with the
  alias transfers above;
- the reviewer's repro and every earlier attack.

Execute every row (`-I`, `sys.path[0]` = case root) with credit and absence variants. Expected: unsound 0. Report every line lost or
won against d71ab79, by cause. Do not add refusal rules beyond rule 3 to pass this.

## F3-F5 (report)
- F3: absence executions swallow exceptions. Count runtime errors per attack from every check, credit and absence, and report the
  totals (the review measured 2,652 cycle and 466 submodule errors per interpreter, not 1,532 and 146).
- F4: the attack spaces above.
- F5: list each duration from round 18's earlier long-`TMUX_TMPDIR` matrix (the review lists them).

## Report
- Fail-before against d71ab79 for each new test.
- Guard per-split counts and the real probe count against d71ab79. List every lost line with the reference that made its file opaque.
  If any use-lrucache line is lost, or a full-files row loses a line, stop and report it.
- The seven modules (tests.test_qual_summarize_code_facts, tests.test_qual_summarize, tests.test_qual_summarize_oracle,
  tests.test_qual_summarize_structured, tests.test_qual_summarize_structured_v2, tests.test_session_env,
  tests.test_qual_summarize_meaning) in all four cells, run one after another:
  - each log starts with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`;
  - record each file's time per cell;
  - run `git reset -q` afterwards, so nothing stays staged.
- Commit nothing. Make `RESULT_STATUS: complete` the last line, exactly. Leave no background shells running. Never send a request to
  127.0.0.1:1234 or any other address from a test or attack script. Use a private `TMUX_TMPDIR` for anything that touches tmux.
