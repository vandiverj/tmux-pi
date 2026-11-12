# GB1: the review gate reads blank context under diff.suppressBlankEmpty (Main, 2026-11-12)

PF1c round 2 (coder-9628d382cb, merged ab722a0; its report.md) found: with `diff.suppressBlankEmpty=true` git writes a blank
context line as an empty line, and `outside_hunks` (`lib/review_rounds.py:569`, the reader `gate` uses) refuses it as "a hunk cut
short", so a small edit whose hunk has a blank context line cannot be reviewed in a repository with that setting. This is on main
before PF1c. `split_sections` (:399) already counts an empty line inside a hunk body as one context line. Lane gate-blank, base
main. Kind: mechanical (Sonnet). Bar: realistic use.

1. `outside_hunks` counts an empty line inside a hunk body as one old and one new line, as `split_sections` does. Counting stays
   by the hunk header only (structure, never content).
2. **The end of the text is not a line.** `gate` passes `text.split("\n")`, whose last element is the empty piece after the final
   newline. That piece must never be counted as a blank context line: a hunk one line short at the end of the text still refuses
   ("ends inside a hunk" or "cut short"), exactly as today. Say how the code tells the two apart, and prove it with a row.
3. Everything else refuses as today: a header, an `@@` line or a non-diff line inside a body; a body longer than its header.

Tests: extend the existing gate/outside_hunks rows in tests/test_review_rounds.py (no new method): a small edit with blank context
under `diff.suppressBlankEmpty=true` through the real `safe_diff` + `gate` path (fails on main: show it once in the evidence-file
form, `.py` in your evidence/ dir run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`), the one-line-short-at-end row from
rule 2, and an empty line outside any hunk (unchanged behaviour). Run `tests.test_review_rounds`, `tests.test_tp_review` and
`tests.test_session_env` once through `tools/agent/test-run.sh`, `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), fresh short `TMUX_TMPDIR` under /tmp,
foreground, one after another.

Edit only lib/review_rounds.py (`outside_hunks` and its docstring) and tests/test_review_rounds.py. Do not edit config/. Search
with safe_grep, read with Read/safe_read; never read .env*, auth files, claude/mcp.json or config/local.json. No network, nothing
to 127.0.0.1:1234, no Claude or pi start, never the default tmux server. Approve no prompt; a placeholder or permission prompt:
stop and report. Commit nothing. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
