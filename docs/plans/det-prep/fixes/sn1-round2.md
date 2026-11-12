# SN1 round 2 (lane small-notes, on a3c0424)

Review review-coder-21c3cc49a8-162459: REQUEST_CHANGES F1 (read its result.md and report.md first). Main's diagnosis: a real
gap in round 1's table, not a brief fault. Round 1 changed three readers (`lib/bench/runner.py` result.md at :381 and :422, the
`transcript-path` pointer near :281-290) but the five-form table only reaches the two result.md readers: the fixture always
supplies an explicit transcript, so the pointer reader is never called.

1. Extend the existing five-form table in `tests/test_bench_runner.py` (same method, more rows; no new method or module) so each
   form (UTF-8, Latin-1, lone byte, empty, missing) also goes through the `transcript-path` fallback. Add a fixture option in
   `tests/bench_fixture.py` if the fallback needs the explicit transcript absent. Assert the reader was reached (the
   reviewer counted zero calls), not only that nothing raised.
2. Show it fails on main 1fb0bfa for the non-UTF-8 rows, in the evidence-file form (`.py` in your evidence/ dir, run as
   `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`).
3. Update report.md's attack table with the pointer-reader rows.

Run `tests.test_bench_runner` and `tests.test_session_env` once each through `tools/agent/test-run.sh`
(`/usr/bin/python3` outside tmux, fresh short `TMUX_TMPDIR`, foreground). No product code change unless the new rows show one is
needed (then say why). Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` last.
