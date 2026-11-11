# PR1: run the whole suite in parallel with the same result as the serial discovery (Main, 2026-11-11)

Owner (2026-11-11): "do both speed up options as the highest priority" (batched full runs; shared setup plus parallel modules).
Today the full suite is `python3 -m unittest discover -s tests -p 'test_*.py'` in one process: ~5,300 tests, 55-67 min. This Mac
has 18 cores and 128 GB. Goal: the same tests, the same verdict, in about 15 min or less. Bar: realistic use (the runner is
Main's and the owner's everyday check; the input is this repo's own test tree).

## The rules
1. **One runner, same tests.** Add `tools/test/run_parallel.py` (stdlib only, Python 3.9+) that runs every test the serial
   discovery runs, split across worker processes. The unit is a test module; a module that alone takes longer than a stated
   threshold may be split by class. Prove the test-id set is identical to `unittest.TestLoader().discover('tests', 'test_*.py')`
   (diff of the two id lists is empty, in the evidence). No test is skipped or run twice that serial discovery would not.
2. **Each worker is isolated the way the serial discovery is.** Every child gets the same scrubbed environment as Main's discovery
   (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), its own fresh `TMUX_TMPDIR` and
   `TMPDIR` under /tmp (short paths: tmux socket paths are length-limited), removed afterwards. It never sets `TP_LIVE_*` or any
   opt-in live variable; it passes through what the caller set, so a live opt-in stays the caller's choice.
3. **Same verdict.** Exit 0 only when every module passed; otherwise non-zero, with each failing test id and its traceback in the
   summary. A child that crashes, hangs past a per-module timeout (stated default, `--timeout`), or prints no unittest result
   counts as a failure for that module, never as a pass. Output ends with a `Ran N tests in S s` line and `OK`/`FAILED (...)` totals
   in unittest's format, so Main's existing log readers keep working, plus per-module time and result.
4. **Longest first.** Schedule by the previous run's per-module times (kept in an untracked file under `.tmux-pi/`; first run with
   no file uses module size or a fixed order, stated). `--jobs N` (stated default, e.g. 8; leave headroom for the owner's own
   work), `--only MODULE...` for a subset, `--serial` to fall back to the one-process discovery unchanged.
5. **Shared-state collisions are found, not hidden.** Run the full suite under the runner and compare with a serial discovery of
   the same tree. Every test that fails in parallel but passes serially is a row in the report: cause (a fixed path, a port, the
   default tmux server, a shared file under the repo, timing under load) and fix. Fix by isolating in the runner when the cause is
   environment; fix the test only when the test itself writes outside its own temp dir (that is a test bug either way), keeping its
   assertions. A short `SERIAL` list in the runner, each entry with its reason, is the last resort; never retry a failed module to
   make it pass.
6. **Known outside failure.** `'utf-8' codec can't decode byte ...` from `tp-agent` is the process-table bug that lane
   process-decode (PD1) fixes; parallel load makes it more likely. Report each occurrence with its test id; do not fix it here.

## Tests (tests earn their place)
One test module for the runner, against a tiny fake tests dir built in a temp dir: a passing module, a failing one, one that
exits without a result, one that hangs past a short timeout. Assert exit code, totals, failing ids, and that the id set equals the
loader's. No test that runs the real suite.

## Docs
Where README.md, docs/project-map.md and docs/plans/det-prep/README.md give the suite command, add the runner as the normal command
and keep the serial command as the reference. One sentence each.

## Verification (the worker runs it once)
Lane base is main (with the process-decode fix not yet merged). Run the runner's own test module once, then the whole suite once
with the runner (`--jobs` default) and once serially, both on the same `git write-tree` tree, each under `perl -e 'alarm 5400; exec
@ARGV'`, outside tmux with the env above. Logs start with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then
`git reset -q`. Report: wall time serial vs parallel, the id-set diff, the collision table, per-module top 15 times. No Claude or
pi start; nothing to 127.0.0.1:1234 or any network; private tmux `-L` sockets only; never `kill-server` without `-L`. Do not
edit config/. Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json.
Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.

## Amendment (Main, answering the worker's request of 15:44Z)
- **Extend the runner that exists; do not add a second one.** `tests/run_parallel.py` (with `tests/run_bounded.py`) is the one
  runner; Main missed it when writing this brief. Keep its path and its CLI (README cites `tests/run_parallel.py -j2`). Bring it up
  to rules 1-6: the id-set check against the loader, the scrubbed env and per-child `TMUX_TMPDIR`/`TMPDIR`, the unittest-format
  totals, longest-first scheduling, a higher `MAX_JOBS`/default chosen from measurement on this 18-core Mac (state the numbers),
  and review each existing `SERIAL` entry against rule 5 (keep only with a measured reason). Extend `RunParallelTests` in
  `tests/test_run_bounded.py` for the Tests section instead of a new module; `tools/test/` is not used.
- **Docs: do not edit them in this lane.** README.md, docs/project-map.md and docs/plans/det-prep/README.md belong to other lanes;
  put the proposed one-sentence changes in your report and Main applies them after merge.
