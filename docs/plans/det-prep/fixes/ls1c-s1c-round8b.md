# LS1c S1c round 8b: a name in a string is a reference (Main, 2026-11-09)

Round 8 (coder-eba72607ca) cut the unsound rows of its attack table to 0, except three: `pydoc.locate`,
`unittest.mock.patch` and `logging.config` load the target from a string, and the file still gets "does not use Q".

## Main's decision: a rule over strings, not a loader list
Every string-built loader needs the target's name as text. The set of loaders is open; the text they need is not.
- **Rule:** an absence line for file F and target Q (in module M) is not emitted when any string constant in F, split on
  `.`, `:` and `/`, has a token equal to `Q` or to the last component of any of M's names (the path-suffix names of
  round 7). A string built only from constants counts too: join adjacent constants, `+` of constants, and the constant
  parts of an f-string.
- This replaces the existing exact-match test (`ast.Constant` whose value equals the target) with one rule. Do not add
  `pydoc`, `mock` or `logging` to `_DYNAMIC_ACCESS`.
- **The limit, stated once:** a name that reaches F from outside its source (a config file, the environment, the
  network) is beyond static proof. Say so in the absence-path docstring, and in the sweep notes. Do not change the
  wording of the absence line.

## Tests
- Add the three loader rows to the attack table, plus: an f-string with a constant part `t.`, a `"t:Q"` entry-point
  string, and `"pkg/sub/t.py"`. All block.
- Keep-the-line rows: an unrelated string, and a string with `Q` as a substring of a longer token (`"QUEUE"`).
- Rows worth keeping go into the existing import-graph table. Fail-before against your round 8 working tree: save
  `git diff` as `evidence/round8.diff` first.

## Report
Rerun the whole attack table on both interpreters, and give the unsound and mismatch counts before and after. Give the
guard per-split counts and the probe count against 17e2661 and against round 8, and list every lost line. If any of
use-lrucache's lines is lost, stop and report it. Then run the seven modules in all four cells after your last edit, with
each file's time, one row per cell. Commit nothing. `RESULT_STATUS` as the last line.
