# HB3a round 2: keep owner rules through archive, failure and recovery; every brief part true (Main, 2026-11-11)

Review review-coder-b118d2d7e9-062221 (REQUEST_CHANGES): `result.md`, `report.md`, `repro.py`, `repro.log` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-b118d2d7e9-062221/`.
The HB3a brief (`hb3a-brief.md`, same dir) still applies. Bar: realistic use. Same file ownership as round 1. Lane base for
fail-before: f3b04be. Kind of round: mechanical fixes to stated rules.

## Main's diagnosis of round 1 (owner rule: every REQUEST_CHANGES is diagnosed)
- Brief fault (Main's): where global and project records live was not stated (F2). Rule 2 below states it.
- Worker: F5, F6, F7 are stated rules not met (brief rules 5 and 6); F1 and F4 were real defects reported as known limits; F3 is a
  storage bug. A defect in normal use is fixed, not listed.

## The rules
1. **No failed write loses an owner rule (F1).** In a cross-workspace supersession (and confirm/answer, which share the path), save
   the replacement first, then mark the old record superseded. A failure between the two leaves the old rule active and the new one
   present but pending; the brief must never show neither. Test with an injected save failure at each step.
2. **Global and project records outlive their workspace (F2).** Records with scope `global` or `project:` are read from every
   workspace file, archived included, until withdrawn or superseded. Archiving never changes a record's status. Workspace, lane,
   task and role records follow their workspace (archived → not in the brief). Say this in docs/persistent-work.md.
3. **Multiline text is one record (F3).** `work decide` / `decision add|answer` with newlines stores one record whose text keeps
   the newlines (escaped in the line form, or the entry form; your choice, stated) and reads back as exactly that record, one status.
   F8 (a hand-written trailer) stays a known limit; if your storage removes that route, say so.
4. **Checkpoints keep records (F4).** The checkpoint copies an entry's `Record:` field and `work recover` restores kind, scope,
   status, supersedes/superseded_by. Old checkpoints without it still validate (they read as round-1 old forms).
5. **Paused work is shown (F5).** The brief's active work includes paused tasks with their result status (e.g. `blocked`), in the
   same shape as running ones.
6. **Each part carries its own time (F6).** Outcome, acceptance, each rule, each open decision: the time the part itself was last
   written (store the time on write if the store lacks it; a part written before this change shows `time unknown` and is never
   called fresh). Never the whole file's update time. STALE compares that time with the newest compaction generation, as the
   reviewer's F10 lookup does.
7. **An unusable store says so (F7).** If the store path is not a readable directory, or listing it fails, the brief's first line is
   `working brief unavailable: work store <path> unusable: <reason>`, exit 0, and it never suggests creating a workspace.
8. **"last evidence" means approved (F9).** Show the newest review whose verdict is APPROVED (from the session's review task records:
   review id, reviewed task, verdict time) and the evidence file that review was given, instead of the newest `main-evidence` file by
   mtime. No approved review → `last approved review: none`.
- **F10 (check):** correct the report: 110 cells / 22 pairs / 132 records / 22 replacements; the freshness claim; the unwritable-store
  row (make it really unwritable or rename the row to what it tests); note the missing bounded runner as a deviation.

## Tests (owner rule: tests earn their place)
Extend the round-1 tests: add rows to the existing attack table, start table and compaction rows, and to `Records`; a new test
method only where no existing one reaches the path, with the reason. One row per review probe in `repro.py`, fail-before against
f3b04be. Report added vs extended.

## Verification (owner rule: the worker tests once)
Run the touched and importing modules once (the round-1 list) plus `tests.test_session_env`, with `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`), under a bounded runner (`perl -e 'alarm 900; exec @ARGV'`),
the log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and
the full discovery. Temp `TP_WORK_DIR` only, never `~/.config/tmux-pi/work`. No Claude or pi start; nothing to 127.0.0.1:1234 or any
network. Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env* or auth files. Commit nothing. No
background shells. `RESULT_STATUS: complete` as the last line.
