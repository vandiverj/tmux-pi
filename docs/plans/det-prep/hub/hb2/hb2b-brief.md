# HB2b: a launch record for every worker launch and for Main (Main, 2026-11-11)

Owner workstream: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/det-prep/hub/owner-request.md` (target: leave and
return and get completion evidence without reminding Main how to operate; any miss must be explainable). Plan: `.../hub/README.md`
(HB2). Shared shape: `.../hub/interfaces.md` section 2 (implement exactly that; if a field is wrong or cannot be filled, say so in
the report and write it as null with a reason, never a guess). HB2a (merged f98b66c; briefs `hb2a-brief.md`, `hb2a-round2.md` in
this dir) delivered the worker contract; its report's "What step b needs" lists the launches that bypass tp-spawn. Bar:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`. Target: Claude Main and its workers (pi
Main later). Kind of round: design (Opus).

## Why
Today the only trace of what a worker was given is the first line of its `system-prompt.md`; `meta.json` has no contract,
role, tool or capability fields, and Main has no record of its own start. `lib/work_brief.py:390` prints "launch record (HB2): no
launch record exists yet". When a worker misuses a tool or misses a rule, nobody can say whether it was told.

## The rules
1. **`TASK_DIR/launch.json`, schema `tp-launch:v1`, on every route tp-spawn serves** (spawn, retry, review, lane step,
   contract, investigate/trial, bench): written by tp-spawn at the point it has the final system prompt and argv, before the
   harness starts, atomically. `instructions` lists each delivered piece (contract, role file, preamble, manifest block, injected
   rules) with path, sha256 of the text actually delivered, and version where one exists. `tools` and `capabilities` come from
   what tp-spawn passes or the adapter declares (one table per harness; reuse HB2a's per-harness table location), not from a
   hand-written list per route. `route` names the route. A launch that fails before the harness starts leaves no record or a
   record marked failed (say which).
2. **Main's record.** `bin/tp-main` writes `.tmux-pi/sessions/<sid>/main-launch.json` (same schema, `role: main`,
   `route: main`) at each start and recovery, naming what Main was started with (prompt file, hooks settings it points to,
   harness, model if known). This records; it does not change any instruction Main receives.
3. **Readers.** `lib/work_brief.py`'s HB2 slot shows the record (contract version, role, route, unsupported capabilities) for
   the tasks it lists and Main's record, replacing "no launch record exists yet". `tp-agent inspect` (or the existing read path
   it uses) shows it if that is a small change; otherwise say what it would take.
4. **Not delivered, stated.** The launches that bypass tp-spawn (HB2a report: Claude Main's Agent-tool children,
   `agent/extensions/assistant.ts` ask_local, `bin/tp-claude-bootstrap`) get no contract in this step. List each with what it
   would take; adding the contract to the Agent-tool guidance Main carries changes Main's instructions and is the owner's
   decision, so do not make it.
5. **No secrets.** The record holds paths, hashes, names and versions, never prompt text, env values, tokens or file contents.
   config/local.json may be named by path at most; never read.

Ownership: `bin/tp-spawn` (off LP1's pi `--model` line and `launch_model` block), `bin/tp-main`, `lib/work_brief.py`, a new
`lib/launch_record.py` if you need one, the tests that cover them. Ask with `tp-agent request` before touching anything else
(`bin/tp-agent`, hooks, `claude/main-prompt.md`, `config/` belong to others or to the owner).

## Attack (realistic-use bar; build the rows by code, print the table)
Every route × harness {claude, pi} × {manifest, no manifest}; a retry of a retry; a review of a lane step; a project with its own
`.tmux-pi/worker-preamble.md`; tp-main start then recovery (record rewritten, not appended); a launch where the harness binary is
missing (no false "started" record); sha256 in the record equals the hash of the delivered system prompt piece. Realistic
first: the record a coder on battle-soccer and a reviewer of a tmux-pi lane would get, shown in the report (rendered, not live).

## Tests (tests earn their place)
Extend HB2a's delivery-table test in tests/test_orchestration.py to check each row's launch.json; extend the existing tp-main and
work_brief tests. A new test file only if no existing module covers tp-main (say which you looked at). Fail-before on the lane base.

## Verification (the worker tests once)
Render launches with tp-spawn's dry/print path or a fake harness that logs its argv (no live Claude or pi start); fresh
`TMUX_TMPDIR` plus a private `-L` socket if tmux is needed; never the default server, never `kill-server` without `-L`. Run the
touched and importing test modules plus `tests.test_session_env` once with `/usr/bin/python3` outside tmux (`env -u TMUX -u
TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`) through `tools/agent/test-run.sh`, `perl -e 'alarm 900;
exec @ARGV'`, the log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`; per-module
times. Main runs the four cells and the full discovery. Report: the record for each attack row (table), files changed, fail-before
output, rule 4's list, deviations, known limits. Search with safe_grep and read with Read/safe_read; never search `bin/` as a
directory or `config/`, `agent/`, `claude/` recursively, name the files; never read `.env*`, auth files, `claude/mcp.json` or
`config/local.json`. Nothing to 127.0.0.1:1234 or any network address. If a guard hook refuses a command, stop and report it; do
not work around it. Do not approve any permission or placeholder prompt; stop and report it. `git add` new files. Commit nothing.
No background shells. `RESULT_STATUS: complete` as the last line, exactly.
