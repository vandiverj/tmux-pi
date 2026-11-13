# HB2c: a launch record's summary says "unknown" for a field the record does not hold

Follow-up to HB2b. Reviews review-coder-47b147fee2-173305 F3 and review-coder-0324c6f45f-180926 F3 (deferred note):
`lib/launch_record.summary` (`lib/launch_record.py:577–587`) renders a field the record does not hold as "none". An error
record (`:633`) holds only schema, status, error and the known task, role and harness. Its summary therefore claims
"contract none, route None, unsupported none", which reads as "no unsupported capabilities" when nothing is known.
Readers: `lib/work_brief.py:617` and `:635` (what Main sees about a worker's launch).

Rule: in the summary, each field is one of three things.
- A value: the record holds it.
- "none": the record holds the field, and it is empty.
- "unknown": the record does not hold the field (key absent, or a container that is not the expected type).

The same rule covers contract (no `instructions` list, versus a list with no contract entry), role, route and unsupported
(no `capabilities` dict, or no `unsupported` list, versus an empty list). A prepared or failed record's summary is unchanged
when it holds every field. FAILED/ERROR prefixes are unchanged.

Tests: extend existing tests that cover `summary` (`tests/test_work_brief.py:494` area, or where launch_record's summary is
already asserted). Add no new test method if an existing one can carry the rows:
- an error record from `:633`'s shape;
- a full prepared record (unchanged output);
- one record with each field present but empty.
Fail-before: show the error-record row failing on main, as a `.py` in evidence/ run with `python3 -I`. Do not edit any tracked
file for it.

Run the modules that import launch_record or work_brief (`tests.test_work_brief`, `tests.test_worker_env`, `tests.test_tp_review`,
`tests.test_main_notify`, `tests.test_orchestration`) once through `tools/agent/test-run.sh` on `/usr/bin/python3`. Run them
outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), with a fresh short
`TMUX_TMPDIR` under /tmp, in the foreground.

Touch only lib/launch_record.py and the test files above. Do not touch the default tmux server. Do not edit config/. Do not
commit. No git notes. No background shells. Write report.md (the rule, file:line, rows with realistic/contrived tags).
`RESULT_STATUS: complete` as the last line.
