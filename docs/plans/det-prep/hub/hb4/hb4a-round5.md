# HB4a round 5 (last): one turn-number rule for pauses; remedies carry the task's session (Main, 2026-11-11)

**Owner decision, 2026-11-11** (asked under the round-4 stop rule): "Rule-first, last round". Round 4's review
(review-coder-526d8c0202-110610, `result.md`, `report.md`, `probes.py`, `test-runs/` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-526d8c0202-110610/`)
found `pause-completion-race` again (F1, F2, F4) and `request-answer-command` again (F3). **This is the last round.** If either
family recurs in its review, HB4a merges as it is with the findings as known limits (moved to HB4b). The HB4a brief and rounds
2-4 (this dir) still apply except where a rule below replaces them. Bar: realistic use. Lane base for fail-before: b317e77
(round 4) for F1-F4.

## Main's diagnosis
Main's brief fault again. Round 4 let the worker pick how a pause names its turn ("result file identity/mtime or a turn
counter; your choice"), and the choice, a hash of result.md's text, cannot tell an older restored report from a new one (F1),
and stops voiding once the reply is withdrawn (F2). Round 5 fixes the rule itself: turn numbers, never the reply's content.

## Rule 1: a pause is void exactly when its turn, or a later one, completed on its own (F1, F2)
1. **The reporter keeps a completed-turn counter.** In `state.json`, `completedTurn` = the highest `promptTurn` whose own Stop
   wrote a reply with a valid RESULT_STATUS line. It only ever goes up. A report restored from `heldReport` or the archive
   (report.sh lines ~130-145, `cp -p`) does not raise it; a withdrawal on the next UserPromptSubmit does not lower it. Claude
   workers: `claude/plugins/tmux-pi/hooks/report.sh`. pi workers: the code that writes pi's `state.json` (find it; the same field,
   the same rule). A state.json without `completedTurn` (written before this round) means 0.
2. **A pause records the turn it interrupts:** `turn` = the `promptTurn` in state.json when the pause attempt is written.
3. **Void** iff `state.completedTurn >= event.turn`. Nothing reads result.md's text to decide it. `reply_turn`, the hash, and
   every `reply` argument used only for voiding go. An event without `turn` is never void (as now).
4. Every reader uses this one predicate: `lib/contract.py` (`live_pauses`, `recorded_pause`, `pause_unconfirmed`, `lifecycle`,
   `task_lifecycle`, `satisfies_dependency`, evaluate's elapsed, `ledger_row`) and the dispatcher's `pauseVoid`/`contractHalted`.
   One Python function and its TS twin; a parity check exists already, extend it.

## Rule 2: what enforcement prints is the reconciled outcome (F4)
`bin/tp-contract` `cmd_enforce` (around 433-437) reports the pause outcome after rule 1 is applied: a pause void by rule 1
prints `"outcome": "void"` (or no pause row; state which), never `confirmed`. `dispatcher.ts` (~1154) renders "paused it" only
for `confirmed`. The event log keeps its `pause/confirmed` history record; only the current output changes.

## Rule 3: every command a notice suggests runs against the task's own session (F3)
Every `tp-agent` command a notice prints (`register.tsx` `agent(row, ...)` and the text from `needs`) names the task's session the
way `tp-agent` already accepts it for another session (find the flag or env; use the one `tp-agent` itself prints in its own
refusals). `needs` comes back from `answer_route`/`retry_admission` already qualified, so the hint never edits command text. A
task in Main's own session prints the same commands as today.

## Tests (tests earn their place)
Extend, do not add files. The finished-meanwhile test gets real reporter sequences driven through report.sh (and pi's writer):
(a) turn 1 completes, turn 2 starts, pause, partial Stop restores turn 1's report: stays paused (F1); (b) pause in turn 1, turn 1
completes (void), turn 2 starts with no tool yet: not paused (F2); (c) pause after an earlier completed turn: stays paused; (d)
completion of the paused turn at each pause_worker boundary: settled, and enforce output not `confirmed` (F4). The existing
state table gets one external-session row: every suggested command carries the task's session and the real admission check
accepts it (F3). Assert lifecycle, dependency and the TS twin against the expected turn, not only against each other. Fail-before
against b317e77 for (a), (b), (d) and F3.

## Report
Fix the round-4 report as F6 says: sweep table header exactly `Site | Disposition | Evidence` with dispositions `fixed`,
`already correct`, `out of scope` or `unresolved`; drop the universal claims F1-F4 disproved; narrow the "extension does not use
pauses" sentence. Tests counted as added / extended / replaced / removed, by name. Attack table (report artifact): every
reporter event order around a pause (UserPromptSubmit, PreToolUse, Stop with and without status, restore, withdrawal) x pause
boundary, for Claude and pi; every suggested command x local / external session.

## Verification (the worker tests once)
Run tests.test_hub_records, tests.test_contract, tests.test_tp_window, tests.test_tp_review, tests.test_pause_classification,
tests.test_claude_plugin_module, tests.test_refusal_remedies, tests.test_session_env and the plugin tests once, with
`/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), short
fresh `TMUX_TMPDIR` under /tmp, under `perl -e 'alarm 900; exec @ARGV'`, through `tools/agent/test-run.sh`, the log starting
with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the
discovery. No Claude or pi start; nothing to 127.0.0.1:1234 or any network; private tmux `-L` socket only. Do not edit config/.
Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. Commit
nothing. No background shells. `RESULT_STATUS: complete` as the last line.
