# HB7a round 3 (lane hub-friction, on 5217f81): unreadable store is silent on Python 3.13+

Main's cells on preview dc672d6 (main 153f22b + 5217f81): py3.9 passes; **py3.14 fails** `test_unreadable_inputs_are_listed`:
`AssertionError: 'record' not found in {'map', 'memory', 'label'}` (log: /Users/jerrickvandiver/.claude/jobs/6061f696/tmp/hb7a2-out/py314-out.log).
Cause (Main's reading, confirm it): `lib/work.py` `iter_docs` lists files with `Path.glob`, and from Python 3.13 `glob` skips a
directory it cannot read instead of raising. So `work.all_records` returns `[]` with nothing skipped, and the report says 0 records
with no problem: the silent failure round 2's rule 2 forbids, on the source HB7 is built for.

## Rule (state it in report.md first)
Readability of every directory the report depends on is checked by an operation that raises on every supported Python (3.9 and
3.14), e.g. `os.scandir`/`os.listdir` on the store root and each directory `lib.work` reads (find them in `_file_path`/`iter_docs`;
do not edit lib/work.py), and likewise for the memory dir and doc/contract directories if they use `glob`/`iterdir` that can skip
silently. A failure is a listed problem for that source; other sources still report. List every `glob`/`rglob`/`iterdir` in
report.py and say why each cannot skip silently now.

## Tests
No new method: the existing `test_unreadable_inputs_are_listed` is the proof. Run `tests.test_friction_report` under **both**
`python3.9` and `python3.14` (find them with `command -v`), plus `tests.test_session_env` and `tests.test_docs_wiring` once,
through `tools/agent/test-run.sh` with a fresh TMUX_TMPDIR. Fail-before: the py3.14 failure above is it; no extra harness.
Do not commit. Do not add git notes.
