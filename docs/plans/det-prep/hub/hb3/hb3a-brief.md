# HB3a: owner decisions as records, and a working brief Main gets at every start and compaction (Main, 2026-11-11)

Owner workstream: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/det-prep/hub/owner-request.md` ("Main loses
purpose", "repeatedly reminding Main how to operate"). Plan: `.../hub/README.md` (HB3). Shared shape: `.../hub/interfaces.md`
section 5 (implement exactly that; if it is wrong, say so in the report instead of inventing another). Audit with file:line:
`.../hub/audit/C-memory-evidence.md` (GOAL 1a and F6). Existing store and its rules: `docs/persistent-work.md`, `lib/work.py`,
`lib/work_commands.py`, `lib/work_checkpoint.py` if present. Bar: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`.
Target: Claude Main only (owner: "lets only worry about claude main for now"); pi Main later. Route rule-first. Kind of round:
design (Opus). This is step a of two; step b (dispatch puts in-scope preferences into worker packets, decision-recording lines in
Main's prompt, lane-cleanup knowledge) builds on this; do not build it now.

## Why
The work store already has workspaces, decisions with `--by user --quote`, checkpoints and `work recover`, but in this campaign it
is empty: nothing attaches a workspace, so 52 compactions carried no checkpoint and owner rules live only in Claude's auto-memory
and prose briefs. Main is reminded of the same rules again and again (audit C, F6). Owner target: "I provide a goal, correct the
process once if necessary, leave and return, and receive current completion evidence without repeatedly reminding Main how to operate."

## The rules
1. **Decision records carry kind, scope, provenance, status and supersession** (interfaces.md section 5) on the existing
   `work decide` lines and `work decision add|answer` entries. Old entries without the fields stay readable: a line with no
   fields reads as `kind: design`, `scope: workspace`, `status: active` if it was written `--by user` with a quote, else
   `provisional`. Say exactly how old lines map, with a test per form found in `lib/work.py`.
2. **Only the owner makes a record active.** `by: owner` with a non-empty `quote` → `active`. Anything else (`main`, inferred,
   no quote) → `provisional` until `tp-agent work decision confirm ID --quote TEXT` (or the equivalent you choose; name it) adds
   the owner quote. Text from a worker (any process with `TP_TASK_ID` set) can never write an `active` record: refuse with a clear
   message, do not downgrade silently.
3. **Supersession is explicit.** `--supersedes ID` marks the old record `superseded` with a pointer both ways; a record that
   supersedes one in a different scope is refused. Withdrawn and superseded records never appear in the brief. Two active records
   in the same scope that answer the same question are a conflict the brief shows (do not pick one).
4. **A workspace is attached without being asked.** When Claude Main starts (SessionStart startup/resume/compact) with no
   attached workspace, attach the newest active workspace whose `repos` covers the session's project; if there is none, the brief
   says "no workspace attached: `tp-agent work new ... && tp-agent work attach ...`" as its first line. Never create one silently.
5. **`tp-agent brief`** prints the working brief (≤ ~60 lines) from records only, per interfaces.md section 5: outcome, acceptance,
   active preferences in scope (global, project, workspace), active work (tasks, lanes, reviews with state, from the session index),
   open decisions, last verified evidence pointer if any, pointers. Each part shows its record time; a part older than the
   newest compaction is marked stale. Obligations (HB5) and launch records (HB2) do not exist yet: leave a named empty slot, do not
   fake them. No model call.
6. **Delivered to Claude Main at every start and compaction.** `claude/plugins/tmux-pi/hooks/sessionstart-compact.sh` (and the
   SessionStart startup path if it is a different file; find it from `hooks.json`, do not edit `hooks.json` lines lane hub-correct
   owns, ask if you must) injects the brief; `precompact.sh` writes a checkpoint of the attached workspace (it already does when
   attached; with rule 4 it now always is, or says why not). Fail open: a broken store never blocks a start or a compaction, and
   the injection then says the brief is unavailable and why.

Out of scope: pi Main; worker packets; `agent/` files; the HB4 files (`report.sh`, `register.tsx`, `bin/tp-contract`,
`lib/hub_records.py`). Keep `bin/tp-agent` edits to the `brief` subcommand registration and its call (lane hub-correct edits the
message path in the same file).

## Attack (realistic-use bar; build the rows by code, print the table)
Records: {owner with quote, owner without quote, main, worker process, old line with no fields, old `--by user --quote` line} ×
{global, project, workspace, lane, task scope} × {new, superseded, withdrawn, confirmed later, conflicting active pair}. Expected
per row: status, whether it shows in the brief for a Main in project P with workspace W, and the brief line. Start rows: {no
store, empty store, one matching workspace, two matching (newest wins, the other listed), only a non-matching workspace, corrupt
workspace file, unwritable store}. Compaction rows: precompact then sessionstart-compact with a decision written in between and
without. Realistic first: the owner rules in audit C F6 written as records, then a brief built from them, shown in the report.

## Report
Fail-before against the lane base for each new test. Tests use a temp `TP_WORK_DIR` and temp state dirs, never
`~/.config/tmux-pi/work`; no tmux needed (if one is, a fresh `TMUX_TMPDIR` plus a private `-L` socket; never the default
server). Run the touched and importing test modules once with `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`), plus `tests.test_session_env`, the log starting with
`date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`; per-module times. Main runs the four cells and
the full discovery on the preview; do not run them. Report: attack table and counts, old-line mapping, the realistic brief, every
reader/writer of decision lines you changed (file:line), what step b needs, deviations, known limits.
Search with safe_grep and read with Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/`
recursively, name the files; never read `.env*`, auth files or `claude/mcp.json`. Nothing sent to 127.0.0.1:1234 or any network
address; no Claude or pi start. `git add` new files. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last
line, exactly.
