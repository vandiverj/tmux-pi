# TR1: task records survive a file that is not UTF-8 (Main, 2026-11-12)

`lib/task_record.py` `_read` (:45) reads with `encoding="utf-8"` and catches only `OSError`. A task file with invalid UTF-8
(request.md, task.md, result.md, escalated: a worker can write any bytes, a pasted log can hold Latin-1) raises
`UnicodeDecodeError` out of every caller (:95, :98, :234, :258, :295, :308, :442, :898-:906), so one bad byte can stop
`tp-agent` listing, collect or record writing for the whole session. `read_json` (:34) is safe (`UnicodeDecodeError` is a
`ValueError`). Lane task-record-utf8, base main. Kind: mechanical (Sonnet). Bar: realistic use.

1. Invalid bytes never raise out of `_read`; decide the decoding (`errors="replace"` or `surrogateescape`) from what each caller
   does with the text (shown to a person, written back into task.md, compared, hashed) and say which and why in report.md. A
   rewrite of task.md must not turn bytes the worker wrote into different bytes in a field that is kept verbatim (state which
   fields are rewritten and what they hold after the round trip).
2. Check the other reads in this file (:111 is config/local.json: name it, never read the file; :591 `review-of` has no encoding
   and catches OSError/ValueError: say whether it is covered) and any reader the same callers rely on in `lib/` that has the same
   gap; list each with its line, fixed or why not.

Tests: extend tests/test_task_record.py (no new module) with a table of byte shapes (lone
continuation byte, truncated sequence, Latin-1 text, 0xFF, a NUL, valid UTF-8 control row) x the files `_read` reads, through the
public functions the callers use. Fail-before on main for the non-control rows, in the evidence-file form (`.py` in your evidence/
dir, `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`). Run the touched test modules and `tests.test_session_env` once through
`tools/agent/test-run.sh`, `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR
-u TP_STATE_DIR`), fresh short `TMUX_TMPDIR` under /tmp, foreground, one after another.

Edit only lib/task_record.py and tests/test_task_record.py. Do not edit config/. Search with safe_grep, read with Read/safe_read; never read
.env*, auth files, claude/mcp.json or config/local.json. No network, nothing to 127.0.0.1:1234, no Claude or pi start, never the
default tmux server. Approve no prompt; a placeholder or permission prompt: stop and report. Commit nothing. No git notes. No
background shells. `RESULT_STATUS: complete` as the last line.
