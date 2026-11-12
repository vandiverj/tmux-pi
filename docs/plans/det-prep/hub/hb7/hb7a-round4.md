# HB7a round 4 (lane hub-friction, on 032e048), rule-first

Review review-coder-42a68701b3-085324: REQUEST_CHANGES F1-F3 (read its report.md). Main's diagnosis: F1 comes from round 2's
wording ("mapping entry" could mean the key or the source; Main meant both). F2: round 3 asked for the store root and the code
probes only its children. F3: round 2 asked for the deferral in help text. Two families recur (owner-quote-attribution,
input-discovery-errors), so write the rule tables first, then code.

## Rules
1. **Attribution has two levels and both apply.** A mapping key's `by` and each source pointer's `by`. If the key's `by` is
   `worker`, the key is never `active` (reason names the key-level attribution) whatever its sources are. If a source's `by` is
   `worker`, that source is `worker`. A source is `owner+quote` only under round 2 rule 1 (HB3 owner record with quote, H48 label
   row) and only when neither level says `worker`. Table: every (key by) x (source by) x (source kind) cell -> source provenance
   and key status. One table-driven set of rows in the existing test covers it.
2. **Every directory the report reads is listed (`os.listdir`) before use, the root included**: the store root, its
   `workspaces` and `archive/workspaces`, the memory dir, `agent/roles`, `agent/contract`. A missing child is fine where
   round 3 said so; any other `OSError` is a listed problem for that source, other sources still report. Table: each directory ->
   probe line -> problem text. Extend the existing permission test with a store root that can be traversed but not listed
   (mode 0o111); skip as root.
3. **`--help` shows the deferral**: grouping comes from the mapping only; a record-local `friction` key is not read (deferred,
   needs lib/work.py and interfaces.md section 5). Put it in the parser's description or epilog, not only the docstring. Check it
   with the existing CLI call in the test (one assertion), not a new method.
4. F4 (list-then-glob window) stays a known limit in report.md.

Fail-before: run the new rows against 032e048 in the evidence-file form (`.py` in your evidence/ dir, `cd "$TP_TASK_DIR" &&
python3 -I evidence/<f>.py`; copies under evidence/). Run `tests.test_friction_report` under python3.9 and python3.14, and
`tests.test_session_env tests.test_docs_wiring` once, through `tools/agent/test-run.sh` with a fresh TMUX_TMPDIR. Do not commit.
Do not add git notes.
