# HB3b: worker packets carry the owner's rules in force (Main, 2026-11-12)

Owner workstream: `docs/plans/det-prep/hub/owner-request.md` ("receive current completion evidence without repeatedly reminding
Main how to operate"). Plan: `docs/plans/det-prep/hub/README.md` HB3 ("dispatch pulls in-scope preferences into the worker
packet"). HB3a (merged 54af2a2; brief `hb3a-brief.md`, rounds 2-3) built the records (`lib/work.py`: kind, scope, status,
supersession; `SCOPE_RE` :660 already accepts `role:<role>` and `lane:<name>`), `tp-agent brief` (`lib/work_brief.py`:
`covers` :75, `in_force` :92, `_rules` :423) and the hooks, and put worker packets out of scope. This is that step. Target:
Claude Main's workers (owner: "lets only worry about claude main for now"). Bar: realistic use (`docs/plans/realistic-use-bar.md`).
Lane hub-packet, base main. Kind: design (Opus). **Adoption is the owner's**: Main stops before the merge.

Why: the owner corrects a worker-facing rule once (example on this Mac: "Committed, portable files never depend on machine-local
state", a global correction record), and today it reaches workers only if Main remembers to paste it into each brief.

Read first: `lib/work_brief.py` (whole), `lib/work.py` records part (from :640), `bin/tp-agent` the dispatch/start path that
writes `--lane-facts-file` (:698-:735: the precedent for a packet section built in tp-agent and passed to tp-spawn),
`bin/tp-spawn` header comments and packet assembly (:25-:80, :520-:600), `docs/persistent-work.md`.

## Rules (tables in report.md first, then code)
1. **Which records.** Active records only, not replaced (reuse `in_force`; do not write a second matcher), whose scope covers the
   worker: `global`; `project:<repo>` when the worker's working directory is that repo **or a worktree of it** (a lane worktree
   has a different path: resolve through `git rev-parse --git-common-dir`, read-only); `role:<role>` for the worker's role;
   `lane:<name>` for the worker's lane; `task:<id>` for the task or the author task it reviews/retries; `workspace` when Main's
   session has that workspace attached. Provisional, pending, withdrawn, superseded and worker-written records never appear.
   An out-of-scope record never appears (negative rows are required: other role, other lane, other project).
2. **Section.** A packet section `# Rules in force` (one line per record: kind, scope label, the rule text, the owner quote clipped,
   the record id), placed after the task and before context files, counted against the packet limit. Conflicts in one scope
   (`in_force` groups) are shown as a conflict line, never resolved. No records -> no section (not an empty heading).
3. **Size cap.** At most 20 lines and 4000 characters; when more apply, the newest first and one closing line naming how many were
   left out and the command that lists them (`tp-agent work records ...`). The cap never cuts a line in half.
4. **Fail open, visibly.** A missing, corrupt or unreadable store never blocks a dispatch: the section is one line saying the rules
   could not be read and why. No model call, no network.
5. **Same rules for every start path.** Dispatch (lane and non-lane), retry, review (reviewer packets: decide whether reviewers get
   the section, recommend, and say why; owner rules about reviews exist), and investigate. Name each start path you found and what
   it does; a path that cannot get the section is listed, not silently skipped.
6. **Records are data.** The rule text and quote go in as text: no placeholder expansion, no shell interpretation; a record holding
   `$(...)`, backticks, `%s`, a leading `!`, `# Task` or a `RESULT_STATUS:` line arrives verbatim and cannot open a new packet
   section (prefix or indent so it cannot pass for a heading; say which).

## Input-space attack table (required in report.md, built by code, one row per cell -> the test row that proves it)
Records: {owner with quote active, main provisional, worker-written, superseded, withdrawn, conflicting active pair} x scopes
{global, project (main checkout), project (lane worktree of it), other project, role match, other role, lane match, other lane,
task match, other task, workspace attached, workspace not attached}. Store: {none, empty, corrupt, unreadable, 0 / 1 / 20 / 21 / 200
matching records, one record of 5000 chars}. Start paths: each found in rule 5. Content: each shape in rule 6. Expected per row:
shown or not, the line, the packet still under its limit. Realistic first: the records on this Mac's store as they are (list them
with `tp-agent work records` read-only, report counts only, no text) and the packet section a coder in lane hub-packet would get.

## Tests (tests earn their place)
Extend the existing dispatch/spawn packet tests and tests for work_brief rather than adding a module; one table-driven method for
the matrix where rows share a fixture (temp `TP_WORK_DIR`, temp state dir, temp repo with a worktree). Fail-before against the lane
base for each new row that is not a negative. Never `~/.config/tmux-pi/work` in a test.

## Verification (the worker tests once)
The touched and importing modules plus `tests.test_session_env` once, `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), fresh short `TMUX_TMPDIR` under /tmp
plus a private `-L` socket if tmux is needed (never the default server), through `tools/agent/test-run.sh`, log starting with
`date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`; per-module times. Main runs cells and discovery.

Do not edit claude/main-prompt.md, agent/roles, agent/contract, hooks, config/. Keep `bin/tp-agent` edits to the start paths.
Search with safe_grep, read with Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/` recursively;
never read `.env*`, auth files, `claude/mcp.json` or config/local.json. Nothing to 127.0.0.1:1234 or any network; no Claude or pi
start. **Guard refusals:** a read-only analysis script goes in your evidence/ dir as a `.py`, run as
`cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`; any other refusal: stop and report. Approve no prompt. `git add` new files.
Commit nothing. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
