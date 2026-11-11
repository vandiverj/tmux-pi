# HB4b: the pause names the turn it interrupts; Stops credit their own turn; notices carry needs as data (Main, 2026-11-11)

Source: `hb4b-known-limits.md` (this dir) K1-K3, from review-coder-09ec6ba638-124935 (`result.md`, `report.md`, probes in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-09ec6ba638-124935/`).
HB4a's brief and rounds 2-5 (this dir) describe the current design; round 5's turn-number rule stays. Bar: realistic use
(owner). Lane hub-correct2, base main. Route rule-first: each rule below is one function or one field, not more cases.

## Rule 1: a pause names the turn current when its key is sent (K1)
`bin/tp-contract` (~306-344) reads `promptTurn` before it saves and sends the pause. The pause event's `turn` is the
`promptTurn` read **after** the interrupt key is delivered and before the event is confirmed; if the turn read before the send
differs from the turn read after, the event records the later turn (and both in the event for the record). A pause whose turn
cannot be read after the send is `unverified`, never `confirmed`. One function computes the pause turn; the Claude and pi paths
both call it.

## Rule 2: a Stop completes the turn its reply answers, not the latest (K2)
`claude/plugins/tmux-pi/hooks/report.sh` (~132, 267-269) credits every statused Stop to the latest `promptTurn`. The reporter
records, at each UserPromptSubmit, the turn that prompt starts; a Stop raises `completedTurn` only to the turn that Stop's reply
started from (the turn current at the start of the reply it ends). With overlapping prompts (the order
tests/test_claude_report.py already allows), turn 1's Stop sets completedTurn to 1, never 2. pi's state writer gets the same rule
(find it; same field). `completedTurn` still only goes up; restores and withdrawals still do not move it.

## Rule 3: `needs` is data end to end (K3)
`claude/plugins/tmux-pi/hooks/register.tsx` (~192-193) re-tokenises the notice text, collapsing whitespace. The suggested command is
built from the structured `needs` (argument list from `answer_route`/`retry_admission`) and quoted once with the shell quoting the
file already uses; nothing parses text back into arguments. A path with two spaces, a tab, a quote or a leading dash round-trips
exactly.

## Attack (input space, report artifact; oracle written from these rules first)
- Rules 1-2: every order of {UserPromptSubmit t1, t2; PreToolUse; Stop with/without status for t1, t2; restore; withdrawal; pause
  send} around a pause at each boundary (before t1 completes, between t1 Stop and t2 prompt, during t2), Claude and pi. Expected:
  paused or void by `completedTurn >= event.turn` with the turns above.
- Rule 3: `needs` paths/args with: two spaces, tab, single and double quotes, `$`, backtick, leading `-`, unicode, newline
  (refuse). Expected: the printed command, run through `shlex.split`, yields exactly the original argument list.

## Tests (tests earn their place)
Extend existing tables, no new files: the finished-meanwhile sequences gain K1's (turn 1 completes, turn 2 starts between the
read and the send: event names turn 2, partial Stop restoring turn 1 keeps it paused) and K2's (overlapping prompts, turn 1's
Stop: turn 2's pause stays) through the real report.sh; the notice table gains one row with the hard path (two spaces + quote).
Fail-before against main for each.

## Verification (the worker tests once)
Run tests.test_hub_records, tests.test_contract, tests.test_claude_report, tests.test_pause_classification,
tests.test_claude_plugin_module, tests.test_refusal_remedies, tests.test_session_env and the plugin tests once (any module that
does not exist: say so), `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR
-u TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 900; exec @ARGV'`, through `tools/agent/test-run.sh`,
the log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Runs under 10 minutes each,
one after another in the foreground. Main runs the four cells and discovery. No Claude or pi start; nothing to 127.0.0.1:1234 or
any network; private tmux `-L` socket only, never the default server. Do not edit config/. Search with safe_grep, read with
Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. **Guard refusals:** if a guard hook refuses a
shell or inline-script edit, make the same edit with the Edit/Write tool and list it in the report; any other refusal: stop and
report. Approve no permission or placeholder prompt. Commit nothing. No background shells. Report: sweep table `Site |
Disposition | Evidence`; tests added / extended / removed by name. `RESULT_STATUS: complete` as the last line.
