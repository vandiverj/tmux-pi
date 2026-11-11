# HB4a round 4: three invariants instead of observation points (Main, 2026-11-11)

Review review-coder-4fe175cf3b-090843 (REQUEST_CHANGES): `result.md`, `report.md`, `probes.py`, `migration-probe.mjs`,
`test-runs/` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-4fe175cf3b-090843/`.
The HB4a brief, round 2 and round 3 (this dir) still apply except where a rule below replaces them. Bar: realistic use. Lane base
for fail-before: ec566e4. Kind of round: **design** for all three families.

## Main's diagnosis (owner rule: every REQUEST_CHANGES is diagnosed)
This is Main's brief fault, not the worker's. Rounds 2 and 3 told the worker *where to look* (take the baseline before the read;
the cursor is what the startup index recorded; check lifecycle before suggesting a command). Each was an observation point; the
reviewer found the next interleaving each time. Round 4 states the invariant each family must hold, wherever the race lands.
- `pause-completion-race` (2nd recurrence): a pause was checked then written; completion can land after the last check.
- `request-notice-loss` (3rd recurrence): recovery paths still promote "the state I first managed to read" to history.
- `request-answer-command` (2nd recurrence): the hint copies part of the command's admission rule instead of asking it.

**Stop rule for this lane.** If round 4's review finds any of these three families again, Main stops HB4a rounds and brings the
owner a scope decision (keep the parts that are approved-quality, move the family to HB4b, or park), instead of a round 5.

## Invariant 1: a finished turn is never reported as paused (pause-completion-race)
A pause record names the turn it interrupted (the result baseline it was taken against: result file identity/mtime or a turn
counter; your choice, stated). Every reader that turns records into a lifecycle (`lib/contract.py` `recorded_pause`,
`task_lifecycle`, `satisfies_dependency`, and any consumer the review lists) treats a pause as void when a completed result for
**that same turn or later** exists. An older result never clears a newer pause (the review's warning: a legitimate pause after an
earlier completion stays). No extra final read in `bin/tp-contract` is the fix; reconciliation on read is. Test: extend the
existing finished-meanwhile test so completion is scheduled at every observation **and** persistence boundary of `pause_worker`
(including pi's waiting-before-result write order) and assert lifecycle and dependency afterwards; plus the row "pause after an
earlier completed turn stays paused".

## Invariant 2: every transition after Main's view is told once, on every start path (request-notice-loss)
The cutoff is the log position at the moment of Main's view, never "the first state I managed to read later".
- Successful start: unchanged (cursor taken with the startup index).
- Failed startup list read: the notifier does not snapshot later. It sets the cursor from a log read made at the same time as
  the index it injects; if no log read succeeds at start, the cursor starts at 0 for that task and requests already resolved
  are told as transitions (an extra closing notice is acceptable; a lost one is not). State which you chose and why.
- Reload: the only saved shapes to support are main's shipped atom (`delivered` of `id:status` keys, no cursors; see
  `git show main:claude/plugins/tmux-pi/hooks/register.tsx` around line 68) and the new one. The lane's round-2 `req:` atom never
  shipped, so it is contrived: drop code written only for it. For main's shipped atom, treat it as the failed-start case above
  (no closing lost; a repeated opening of a still-open request at most once, stated as a known limit in the report).
- No path calls the history snapshot after the start cutoff.
Test: extend the existing startup/reload table (one Node invocation, no new file) with: failed list + successful index + A
resolves before first poll (closing told); main-shipped atom with A open, and with A closed meanwhile; assert ids rendered.
Fold the round-3 second scenario table's quick/old rows into the existing table (review F5).

## Invariant 3: a suggested command is one the command itself would accept now (request-answer-command)
The hint asks the command's own admission check, not a copy: factor `bin/tp-agent`'s retry admission (`guard_retry`, the
repeated-review route gate, and the message refusal) into one function both the command and `answer_route` call. When it would
refuse, the notice names the prerequisite the refusal text names (e.g. stop the live process with the command it prints) or
`tp-agent inspect <id>`; never a command that would refuse. Test: extend the existing state table with a failed pi
worker whose process is alive and a collected author with a live peer on its tree, using the real guard (fake only the pane/process
table); remove the `guard_retry` stub at `tests/test_hub_records.py:495`.

## Report (F6)
Sweep table header exactly `Site | Disposition | Evidence`. Tests counted as added / extended / replaced / removed, by name.
Attack table (report artifact, not one test per row) covering each invariant's input space: every boundary for 1, every start
path for 2, every task state x admission refusal for 3.

## Verification (the worker tests once)
Run tests.test_hub_records, tests.test_contract, tests.test_tp_window, tests.test_tp_review, tests.test_pause_classification,
tests.test_claude_plugin_module, tests.test_session_env and the plugin tests once, with `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp, under
`perl -e 'alarm 900; exec @ARGV'`, the log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then
`git reset -q`. Fail-before against ec566e4 for each invariant. Main runs the four cells and the discovery. No Claude or pi start;
nothing to 127.0.0.1:1234 or any network; private tmux `-L` socket only. Do not edit config/. Search with safe_grep, read with
Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. Commit nothing. No background shells.
`RESULT_STATUS: complete` as the last line.
