# HB4c (design): with overlapping prompts, each reply completes its own turn

Owner's OK to change the hook (2026-11-12): "HB4b F1 fix it".

Source: review-coder-ebf698c5ec-175241 F1 (medium, realistic; read its report.md section F1). Event order: UserPromptSubmit t1,
UserPromptSubmit t2, statused Stop, statused Stop. After both Stops `completedTurn` is 1, not 2. So a pause that names turn 2
stays live after turn 2's own statused completion, and a dependency waiting on it is not released.

Cause:
- `claude/plugins/tmux-pi/hooks/report.sh:245-250, 270, 284-286` tracks one open reply (`replyTurn`). The first Stop sets
  `settledTurn` to the latest `promptTurn` (2), which consumes every open prompt. The second Stop is then inactive.
- pi has the same assumption (`agent/extensions/reporter.ts:217-222, 281-283`, one `replyOpen`).
- `tests/test_contract.py:1032-1049` and `tests/test_claude_report.py:360` allow overlapping prompts but never test two
  completions.

## Step 1: find out which orders really happen (before choosing the rule)
Read real records and report what you find. Sources:
- `promptLog` / `promptSubmits` in Claude workers' `state.json` under `.tmux-pi/sessions/*/tasks/*/`;
- transcripts at the path in `transcript-path`.

For each source, report how often a second UserPromptSubmit arrives before the first reply's Stop. When it does, report
whether one Stop or two follow. The answer matters because Claude Code can inject a prompt typed mid-turn into the running
turn. That gives one Stop for two prompts, and its reply then answers both.

Do not start any live Claude or pi session. Do not send anything to LM Studio. Read only.

## Step 2: the rule
State it in report.md before the code. It must be correct in both cases:
- **(a) Two Stops:** each Stop completes the turn its reply started from. The second Stop completes turn 2.
- **(b) One Stop answers both prompts:** after it, the worker is not left "working" forever with turn 2 open.

If the records cannot tell (a) from (b) at the time of the Stop, say so. Then choose the rule that never reports a turn
completed before its reply exists, and never leaves the worker stuck in a working state. Write down what that rule costs
(for example, turn 2 completes only at the next Stop, or is closed as `settled` without a completion).

Keep everything HB4b fixed:
- duplicate-Stop handling;
- completedTurn only goes up;
- K2 (an older Stop never completes the newest prompt);
- a pause is void once completedTurn reaches it (`lib/contract.py` pause_void).

Apply the same rule in report.sh and in reporter.ts. reporter.ts is a pi file; change only what the shared rule needs.

## Tests
Extend the existing overlapping-turn test, with no new test file:
- the (a) sequence through the real report.sh and through reporter.ts: two distinct statused Stops and a turn-2 pause, which is
  void after turn 2's Stop;
- the (b) sequence, if your rule treats it differently;
- the existing duplicate-Stop row.

About four hook calls plus one node process per sequence. Fail-before: a `.py` in evidence/, run with `python3 -I` against
main extracted to /tmp. It names the failing assertions. Do not edit any tracked file for it.

Attack table in report.md over the event orders: prompts 1..3, Stops 0..3, statused or not, duplicate Stop, pause before or
after each event. Mark each row realistic (seen in step 1's records) or contrived.

Run once on `/usr/bin/python3` through `tools/agent/test-run.sh`: `tests.test_claude_report`, `tests.test_contract`,
`tests.test_pause_classification`, `tests.test_hub_records` and `tests.test_session_env`. Run outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), with a fresh short `TMUX_TMPDIR`
under /tmp, in the foreground. Tests use a private `-L` socket. Never touch the default tmux server.

Touch only claude/plugins/tmux-pi/hooks/report.sh, agent/extensions/reporter.ts, tests/test_claude_report.py and
tests/test_contract.py. Ask Main first if you need anything else. Do not edit config/. Do not commit. No git notes. No
background shells. `RESULT_STATUS: complete` as the last line.
