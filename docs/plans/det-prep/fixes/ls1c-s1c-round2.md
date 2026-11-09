# LS1c S1c round 2: close review F1-F7 and F11 (Main, 2026-11-09)

Round 1 (coder-f8f92bfdf7) was reviewed REQUEST_CHANGES. The review is at
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-f8f92bfdf7-150454/result.md,
with the reviewer's reproductions in `repro.py` and `repro.log` in the same directory. Read all three first. The
round 1 brief (docs/plans/det-prep/fixes/ls1c-s1c-code-facts.md) still applies. Wherever this file says more, this file wins.

The rule behind every fix: a code line states only what the code shows. A wrong code line is worse than a missing one,
because the model repeats it as fact. When a case cannot be decided syntactically, emit nothing for it. Never guess.

## Fix (each with a test that fails on the round 1 tree, 312ca30)
- **F1 (true absence dropped):** a claim whose subject is a file (`cli.py does not define pop`) is scoped to that file.
  The claim is false only if the name is defined in that file. The round 1 test at
  tests/test_qual_summarize_code_facts.py:162 expects the wrong result, so fix the test and say so in the report.
- **F2 (identifier detection):** find identifiers in a claim's words, not only in backticks or subjects. A bare word
  that equals a name defined in the named files (`Q`, `attempt`) counts. Keep it from matching ordinary English words
  that happen to be defined names: a word that is also in a small stop list (`a`, `the`, `is`, `not`, `use`, `uses`,
  `file`, ...) or is all lowercase and under 3 characters is not a name unless it is backticked. Test both directions.
- **F3 (equal values):** compare literal values with `ast.literal_eval` equality where both sides evaluate, and fall
  back to the representation only when they do not. `{'a': 1, 'b': 2}` vs `{'b': 2, 'a': 1}` gives no fact.
- **F4 (nested scopes):** calls inside a nested `def`/`lambda`/`class` body belong to that inner scope, not the outer
  function. Either report them under the inner name (`outer.inner`) or not at all. Never report them under the outer name.
- **F5 (use-site recall):**
  - `from t import *` followed by `Q()` is a use of Q from t.
  - Dunder methods the target defines (`__init__`, `__enter__`, ...) are not excluded when called explicitly.
  - If a case stays undecided, emit no "does not use" line for that file. Absence claims need the strongest evidence.
- **F6 (duplicate definitions):** when a target name is defined in more than one file, a file that imports it from
  one specific module (`from t import Q`, `import t` then `t.Q`) still gets its use facts, attributed to that module.
  Skip only the files whose binding is ambiguous, and give those no "does not use" line either.
- **F7 (version pairs):** pair `a.py`/`a2.py` across directories (`old/`, `new/`, `v1/`, `v2/`, `before/`, `after/`).
  Treat `a.py` vs `a_v0.py` as a pair with `a.py` as the older one only if no other reading fits. Document the rule in
  the function's docstring. Add tests for both shapes the reviewer gave, and for a non-pair that must stay unpaired.

## Report
- **F11:** say exactly what the round 1 fail-before log shows: `Ran 12 tests`, `FAILED (errors=21)`, a failing class
  setUp, and one test that passes on base by design. Then give this round's fail-before: each new or changed test, run
  against 312ca30, with the script and log under `evidence/` in the task dir, named by absolute path.
- **F8, F9 and F10 are notes.** F10 (summarize_run not copying `code_facts`) is outside this lane, so do not fix it here.
  F9: add the missing dropped-constant assertion.
- No fixture special-casing. The tune chores are not tests of this code.

## Done means
1. F1-F7 fixed with tests, F9's assertion added, F11 reported.
2. tests.test_qual_summarize_code_facts, tests.test_qual_summarize, tests.test_qual_summarize_oracle,
   tests.test_qual_summarize_structured, tests.test_qual_summarize_structured_v2 and tests.test_session_env pass with
   `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside tmux
   (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside
   (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`). Rerun every module after the last edit and list each file's time.
3. `RESULT_STATUS` on its own line.

No live model and no request to 127.0.0.1:1234. Never read `.env*`, and never write that string in a shell command. Never
search `bin/` as a directory, or `config/`, `agent/` or `claude/` recursively. Any test that calls tmux sets `TMUX_TMPDIR`
to a fresh short dir and uses a private `-L` socket.
