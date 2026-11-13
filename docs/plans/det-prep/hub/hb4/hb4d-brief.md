# HB4d (design): a duplicate of an older Stop after a new prompt does not complete the new turn

The owner started this on 2026-11-12 ("HB4d"). The owner's OK for report.sh edits covers this step.

Source: review-coder-2d72f4c144-204159 F2 (low, pre-existing, traced, not run), recorded as HB4c-L2 in
`docs/plans/det-prep/hub/hb4/hb4b-known-limits.md`. The order is: prompt 1, statused Stop 1, prompt 2, then a byte-equal
duplicate of Stop 1's reply. `claude/plugins/tmux-pi/hooks/report.sh:127-149` checks `already_kept` only when no prompt is
open. With turn 2 open, the duplicate is taken as turn 2's own Stop. It completes turn 2 (voiding a turn-2 pause and releasing
dependents) before turn 2's reply exists, and the worker reads `waiting`.

## Step 1: find out which orders really happen (read only, before the rule)
Reuse the HB4c step-1 scripts if they help. They are in
`.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/coder-2d72f4c144/evidence/step1_*.py`. Read the 419 Claude workers'
`state.json`, their `results/` kept replies, and their transcripts. Print counts, times and hashes, never message text.
Report:
- how often a duplicate Stop arrives at all (same `last_assistant_message` bytes, a second Stop), and how often it arrives
  after a new prompt;
- how often a worker's reply to turn N+1 is byte-equal to an earlier kept reply. Examples are a short ack, a repeated
  "RESULT_STATUS: complete" reply, or the same refusal text twice. Such a reply is legitimate, and a dedup rule would leave
  that turn open.

Do not start any live Claude or pi session. Send nothing to LM Studio.

## Step 2: the rule
State it in report.md before the code. Both cases must hold:
- **(a) A real duplicate of an older reply after a new prompt** does not complete the new turn.
- **(b) A legitimate reply to turn N+1 that is byte-equal to an earlier one** never leaves the worker stuck working. Say
  whether it completes turn N+1, and what that costs.

If the hook's state and inputs cannot tell (a) from (b), say so. Then choose the rule that is never early in the realistic
orders and never leaves a worker stuck. Write the cost down, as HB4c did for L1.

Keep everything HB4a/b/c fixed: duplicate-Stop handling with no open prompt, `owedTurn` (HB4c), completedTurn only goes up, K2,
and the RO1 report rules.

Check whether pi has the same gap. A pi agent_settled with no new assistant message since agent_start is the nearest case;
see `agent/extensions/reporter.ts` `freshReply`. Change reporter.ts only if the shared rule needs it.

## Tests
Extend the overlapping-turn tests HB4c extended (`tests/test_claude_report.py`, and `tests/test_contract.py` if the
dependency side changes). No new test file. Fail-before: a `.py` in evidence/, run with `python3 -I` against main extracted to
/tmp. It names the failing assertions. Attack table in report.md over prompt/Stop orders with duplicate, byte-equal-legitimate,
empty and statused/unstatused replies. Mark each row realistic (seen in step 1) or contrived.

Run once on `/usr/bin/python3` through `tools/agent/test-run.sh`: `tests.test_claude_report`, `tests.test_contract`,
`tests.test_pause_classification`, `tests.test_hub_records`, `tests.test_reporter_usage` and `tests.test_session_env`. Run
outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), with a fresh short
`TMUX_TMPDIR` under /tmp, in the foreground. Tests use a private `-L` socket. Never touch the default tmux server.

Touch only claude/plugins/tmux-pi/hooks/report.sh, agent/extensions/reporter.ts, tests/test_claude_report.py and
tests/test_contract.py. If a guard refuses an edit, ask Main; do not route around it. Do not edit config/. Do not commit. No
git notes. No background shells. `RESULT_STATUS: complete` as the last line.
