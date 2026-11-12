# TR2: three task-file readers outside task_record survive non-UTF-8 bytes (Main, 2026-11-12)

TR1 (lane task-record-utf8) made `lib/task_record.py` read task files that are not UTF-8. Its review
(review-coder-82907407c8-135404, F3) found three more readers of the same worker-written files that still decode strictly
and do not catch the error. A worker can write any bytes to request.md or escalated, and a pasted log can hold Latin-1. Lane
task-reader-utf8, base main. Kind: mechanical (Sonnet). Bar: realistic use.

1. `bin/tp-agent:189` (`record`): `assignment` reads request.md with `read_text()` (locale encoding, strict). It feeds
   `tp-agent` status/list output, so one bad byte stops the listing.
2. `lib/model_router.py:457` (`review_plan`): reads request.md as UTF-8 and catches only OSError, so a review cannot be
   planned for that task.
3. `lib/contract.py:817` (`ledger_row`): `escalated` is read with `read_text()` (strict). It is called by bin/tp-contract:386
   and bin/tp-ledger:56.

For each one, never raise on bad bytes. Decode with `errors="replace"` where the text is shown or stored as JSON (all three
are). Be explicit about `encoding="utf-8"` so the locale cannot change the result. Keep the rest of each function as it is. If
you find another reader of request.md, result.md, task.md or escalated in `bin/` or `lib/` with the same gap, list it in
report.md with its line, and say whether it is fixed or why not. Fix it only if it is a one-line change of the same form.

Tests: extend the nearest existing test for each reader. Do not add a module. Do not add one method per reader if one table
fits. Candidates: tests/test_t9_small_faults.py, tests/test_contract.py (ledger_row), tests/test_model_routing.py or
tests/test_tp_review.py (review_plan). Use a lone 0xFF and a Latin-1 row, through the function or CLI the callers use. Show that
the rows fail on main in the evidence-file form (`.py` in your evidence/ dir, run as
`cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`). Run the touched test modules and `tests.test_session_env` once each through
`tools/agent/test-run.sh`:
- use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground, one after another.

Edit only bin/tp-agent, lib/model_router.py, lib/contract.py and the test modules you extend. Do not touch lib/task_record.py
(lane task-record-utf8). Do not edit config/.

Search with safe_grep and read with Read or safe_read. Never read .env*, auth files, claude/mcp.json or config/local.json.

Do not use the network, send nothing to 127.0.0.1:1234, start no Claude or pi, and never touch the default tmux server.

Approve no prompt. A placeholder or permission prompt means stop and report.

Commit nothing. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
