# TR1 round 3 (lane task-record-utf8, on f18df6a)

Review review-coder-6fe2ad8835-140331: REQUEST_CHANGES F1. Read its result.md, report.md and `evidence/fallback.py` first.
Main's diagnosis: this is a real test gap, and the round-2 brief asked for it ("the request fallback"). Round 2's production fix
is correct (the reviewer agrees). Only the table is short: if the old extractor is restored, every row still passes.

0. **The rule (family verbatim-newline-loss, named by both reviews):** every field task_record keeps verbatim has a byte-exact
   row through every public path that reads it back. List in report.md each kept field (legacy request.md copy, fenced
   request, request fallback, stop-mode output) x each public reader, and point to the row that covers each pair. A pair with
   no row is a gap. Close it here.
1. **Tests only:** extend the existing table with rows where request.md is absent, so the request comes from the canonical
   task.md fenced block. Use CRLF, bare CR, each with a bad byte, and the six byte shapes. Assert the exact bytes returned by
   the public fallback the callers use (`request_text` or whatever the reviewer's fallback.py calls).
2. Show that these rows fail with the round-1 extractor in the evidence-file form: put a `.py` in your evidence/ dir and run it
   as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py` against a copy of 0d8e310's lib. Show they pass on f18df6a.
3. Change lib/task_record.py only if a row shows a real defect, and say so in report.md.

F2 (strict readers outside this file) is lane TR2's job. Do not touch it.

Tests: run `tests.test_task_record` and `tests.test_session_env` once each through `tools/agent/test-run.sh`, as in the brief.
Edit only tests/test_task_record.py (and lib/task_record.py per rule 3). Do not edit config/. Commit nothing. No git notes. No
background shells. `RESULT_STATUS: complete` as the last line.
