# LS1c S1c round 3b: no constant comparison that a mutation or dynamic write could make wrong (Main, 2026-11-09)

Round 3 (coder-eb5bf6b02c) was checkpoint-committed by Main as the lane tip. Its report lists, under Uncertainty, cases that
can still give a wrong line: mutation by method call (`D.update(...)`), and dynamic writes (`globals()[...]`, `setattr`,
`exec`). Close them before review, with the same positive-proof rule as round 3
(docs/plans/det-prep/fixes/ls1c-s1c-round3.md; read it and the round-3 report in tasks/coder-eb5bf6b02c).

## Do
1. A constant is compared only if its literal is immutable: numbers, strings, bytes, `True`/`False`/`None`, and tuples or
   frozensets built only from those. For a mutable literal (dict, list or set) give no line. An annotated constant that passes
   round 3's single-binding test may be compared again, if that is simple.
2. In a file that uses `globals`, `vars`, `locals`, `setattr`, `delattr`, `exec`, `eval`, `__import__`, `importlib` or
   `__dict__` anywhere, the version diff and the use scan state nothing about that file's names.
3. Report the guard and probe counts again (round 3: holdout 15, tune 11, real 0; probe 327).

## Tests
- Add rows to the round-3 binding-form tables: `D.update`, `D.append`, `D[k] = v` (already listed), and each of the
  dynamic forms in item 2. Each one gives no line.
- An immutable `X = 1` vs `X = 2` still gives its line.
- Fail-before against the round-3 tip. Put the script and log under `evidence/` in the task dir and name them by absolute
  path.

## Done means
The same modules and matrix as round 3, rerun after your last edit, with each file's time. Include a Self-review section and
SLOW_TESTS. `RESULT_STATUS` on its own line, as the last line of your final message. Do not start background watchers that
outlive your work.

No live model and no request to 127.0.0.1:1234. No special-casing of fixtures. Never read `.env*`, and never write that
string in a shell command. Never search `bin/` as a directory, or `config/`, `agent/` or `claude/` recursively.
