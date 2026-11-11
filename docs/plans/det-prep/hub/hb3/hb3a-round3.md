# HB3a round 3: an unfinished supersession is pending, and confirm finishes it (Main, 2026-11-11)

Review review-coder-5571b6cc3c-070200 (REQUEST_CHANGES): `result.md`, `report.md`, `repro.py`, `repro.log` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-5571b6cc3c-070200/`.
The HB3a brief and the round-2 brief (`hb3a-round2.md`, this dir) still apply. Bar: realistic use. Same file ownership. Lane base
for fail-before: 9d09e35. Kind of round: one mechanical fix to a stated rule (Sonnet).

## Main's diagnosis of round 2
- Brief fault (Main's): round-2 rule 1 said "the new one present but pending" without saying what makes a record pending when the
  owner already confirmed it (status `active`). The rule below states it.
- F2 (full discovery) is Main's; Main runs it on the round-3 preview. Not the worker's.

## The rule (F1)
A supersession is **committed** only when the old record carries `superseded_by` = the new id (status `superseded`). Until then:
1. **The old rule stays in force.** `in_force` (lib/work_brief.py) counts a record as replaced only when an active record supersedes it
   **and** it is marked superseded by that record. An active record whose `supersedes` target is still active and not back-linked is
   shown as pending: the brief keeps the old rule in force and lists the new one as `pending: replaces <old id>, not yet committed;
   finish with tp-agent work decision confirm <new id> --quote ...` (wording your choice, the ids and the command required).
2. **Confirm finishes it.** `decision confirm <new id>` on an active record whose supersession is not committed writes the missing
   backlink and returns `changed: true` with the supersession note, instead of `changed: false`. Owner words are still required.
   Same for `decision answer` where it shares the path.
3. **Withdrawing the uncommitted replacement leaves the old rule active** (correct: it was never replaced). Say so in
   docs/persistent-work.md under "Writes that fail", replacing "present but pending" with the committed/pending definition and the
   completion command.
4. First-save-failure safety stays as is (nothing written, old active). The same-workspace case (one save) is unchanged.

## Tests (tests earn their place)
Extend the existing failure-injection test and attack table: the second-save failure row checks (a) the brief shows the old rule in
force and the new one pending, (b) `confirm` after the failure is cleared commits and returns changed:true, (c) withdraw of the
pending replacement leaves the old rule in force. One row per `repro.py` probe, fail-before against 9d09e35. Report added vs
extended.

## Verification (the worker tests once)
Run tests.test_work_brief, tests.test_work_decisions, tests.test_work_checkpoint, tests.test_sessionstart_compact,
tests.test_work_cli and tests.test_session_env once, with `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`), under `perl -e 'alarm 900; exec @ARGV'`, the log starting
with `date -u`, `TMUX_TMPDIR`, and `git add -A && git write-tree`, then `git reset -q`. Temp `TP_WORK_DIR` only, never
`~/.config/tmux-pi/work`. No Claude or pi start; nothing to 127.0.0.1:1234 or any network. Search with safe_grep, read with
Read/safe_read; never read claude/mcp.json, .env* or auth files. Commit nothing. No background shells. `RESULT_STATUS: complete` as
the last line.
