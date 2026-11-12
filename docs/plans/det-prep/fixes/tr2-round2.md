# TR2 round 2 (lane task-reader-utf8, on 02081b2)

Review review-coder-bae217b8fc-140709: REQUEST_CHANGES F1. Read its result.md, report.md and `evidence/readers.py` first.
Main's diagnosis: real. Round 1 fixed a fourth reader, `retry` at bin/tp-agent:1695, which the brief allowed. The table only
covers the three named readers, so if retry's decoder is reverted the test still passes.

1. **Tests only:** extend the existing table in tests/test_t9_small_faults.py with retry's replayed request, using the same
   0xFF and Latin-1 rows. Capture the dispatch it would make. Start no worker process. Assert the replayed text is the
   replaced text, not a crash.
2. Show that the row fails with retry's old decoder in the evidence-file form: put a `.py` in your evidence/ dir and run it as
   `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`.
3. Every reader this lane changed now has a row. Say so in report.md as a reader x row list.

Not here: the bench readers (lib/bench/runner.py) and suite speed.

Tests: run `tests.test_t9_small_faults` and `tests.test_session_env` once each through `tools/agent/test-run.sh`, as in the
brief. Edit only tests/test_t9_small_faults.py. Do not edit config/. Commit nothing. No git notes. No background shells.
`RESULT_STATUS: complete` as the last line.
