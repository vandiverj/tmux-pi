# TR1 round 2 (lane task-record-utf8, on 0d8e310)

Review review-coder-82907407c8-135404: REQUEST_CHANGES F1, F2 (read its result.md and report.md first; the reproduction is its
`evidence/roundtrip.py`). Main's diagnosis: both are real and inside the brief. Rule 1 asked for verbatim bytes in kept fields;
`Path.read_text` also translates CRLF and bare CR to LF (universal newlines), which the round-1 table did not try. The test
table named "the files `_read` reads", and the contract artifact is one of them. The brief stands.

1. **F1: verbatim fields keep their newline bytes too.** Every surrogateescape read (legacy task.md migration, the request
   fallback, stop-mode result capture) reads without newline translation (for example `open(..., newline="")`), and
   `_extract_fenced_request` (:76-96) does not rebuild a kept payload with `splitlines`/`join` in a way that changes CR or CRLF.
   Display, parse and compare paths may keep normalising. State in report.md, per kept field, what bytes it holds after the
   round trip, now including CRLF and bare CR.
2. **F2: the contract artifact row.** Add the six byte shapes for a contract artifact through a public `write_record` call
   with `contract.artifact` set, with a status header before the shape so `_artifact_status` still finds it.
3. **Tests:** extend the existing table (same method, more rows): multi-line CRLF and bare-CR payloads (with and without a bad
   byte) across legacy migration, the request fallback and stop output, plus the F2 rows. The CR rows must fail on 0d8e310: show
   it in the evidence-file form (`.py` in your evidence/ dir, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`).

Not in this lane (Main has queued it as a follow-up, do not edit): F3's strict readers `bin/tp-agent:189`,
`lib/model_router.py:457`, `lib/contract.py:817`.

Tests: run `tests.test_task_record` and `tests.test_session_env` once through `tools/agent/test-run.sh`, as in the brief.
Edit only lib/task_record.py and tests/test_task_record.py. Do not edit config/. Commit nothing. No git notes. No background
shells. `RESULT_STATUS: complete` as the last line.
