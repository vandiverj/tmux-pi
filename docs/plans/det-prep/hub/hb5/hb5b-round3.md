# HB5b round 3 (lane hub-main-runs, on ee7cbef), rule-first (family main-worker-coverage, second time)

Review review-coder-846392729c-164350: REQUEST_CHANGES F1 (read its result.md and report.md). Main's diagnosis: real, an
ordering edge the round-2 rule did not spell out ("earlier by end time" was implemented with `_order`, which breaks equal
end times by task name).

**The rule (state it in report.md first, then code to it):** a worker or reviewer run R makes a Main run M a rerun only when
R's end time is strictly less than M's end time, compared as numbers. Equal end times, or an end time that is missing or
unreadable on either side, never make M a rerun (count 0 for that pair). The order used for display and for grouping plain
repeats is unchanged; only Main-rerun attribution uses this comparison. Apply it to exact and covered checks alike.

Tests: extend the existing relation table (no new method) with: equal end times (exact and covered) -> 0; R one microsecond
earlier -> counted; R later -> 0; a missing end time on either side -> 0. Fail-before of the equal-time rows on ee7cbef in the
evidence-file form (`.py` in your evidence/ dir, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`).

Run `tests.test_check_repeats` and `tests.test_session_env` once each through `tools/agent/test-run.sh` (`/usr/bin/python3`
outside tmux, fresh short `TMUX_TMPDIR`, foreground). Touch only tools/checks/repeats.py and tests/test_check_repeats.py. Do not
edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` last.
