# HB4a: stable request identity, delivery separate from resolution, pause only when confirmed (Main, 2026-11-11)

Owner workstream: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/det-prep/hub/owner-request.md` item 4. Plan:
`.../hub/README.md` (HB4) and the shared shapes in `.../hub/interfaces.md` sections 3 and 4 (implement exactly those; if one is
wrong, say so in the report instead of inventing another). Audit with file:line: `.../hub/audit/B-control.md`. Bar:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`. Route rule-first. This is step a of two:
the `tp-agent correct` command itself is step b and builds on what you make here; do not build it now.

## Why
Main sends corrections into workers whose state it has not checked; a confirmed message marks any pending capability request
"answered" whatever it said; notifications to Main key on task:status so two requests between polls become one notice and a
stale one re-notifies after a restart; `tp-contract` records a pause before Escape is sent and never checks it worked. Each of
these makes a correction lost, duplicated or falsely confirmed (audit B, "Other ways").

## The rules
1. **Every request has a stable id.** When a Claude worker enters a permission prompt (`report.sh` state `permission`), when a
   worker files `tp-agent request` (capability), and when a pi worker reports waiting for input, the writer creates
   `TASK_DIR/requests/<req-id>.json` per interfaces.md section 4 (`req-` + a ULID or an equivalent sortable unique id; stdlib only,
   Python 3.9 compatible) and appends `request.opened` to `TASK_DIR/events.jsonl` (section 3). The same prompt seen again by a later
   hook call is the same request (match on kind + `text_sha256` while it is open); a new prompt after one was resolved is a new id.
   Leaving the permission state (any later tool call, or PermissionDenied/Stop) resolves the open permission request with
   `resolution.how` = `left-state` and the event. Route Claude's `PermissionDenied` hook to `report.sh` so a denied prompt leaves
   `permission` (audit B: `hooks.json:8`).
2. **Delivery never resolves a request by itself.** `tp-agent message` records the delivery id it already sends (MS2) in
   `messages.jsonl` and an event `correction.delivered`/`…unverified`, and no longer flips `capability-request.json` to answered.
   A new option `--resolves REQ_ID` resolves that request (by `main`, how `message`, ref = delivery id) only when the delivery is
   confirmed; unconfirmed leaves it open and says so. Keep `capability-request.json` readable for old readers (status mirrors the
   newest capability request), but the request files are the record.
3. **Notifications key on the request id.** `claude/plugins/tmux-pi/hooks/register.tsx` (and any other notifier you find; list
   them) announces each open request id once, announces its resolution once, and never re-announces a resolved id after a restart
   (state kept where the notifier keeps state now). Tasks without request files keep today's behaviour.
4. **A pause is recorded only when confirmed.** In `bin/tp-contract` the pause event is written as `pause.requested` before the
   Escape, and `pause.confirmed` only when a check after it shows the worker stopped (Claude: the pane shows the input prompt and the
   state is not `tool`/`working` within a bounded wait; pi: the reporter's state). If it cannot be confirmed: `pause.unverified`, the
   contract's dependency hold stays in force, the task is **not** shown as paused, and the next enforce tick may retry, at most N
   times (N in config default, say which). Keep the existing behaviour that a failed send does not loop every few seconds.
5. Tool names in the events you write go through one normalising table (section 3) in a new shared module (`lib/hub_records.py`,
   the module later lanes extend). Python 3.9, stdlib only; the shell hooks call it with `/usr/bin/python3` as report.sh does now.

Out of scope here: pi permission requests (`agent/extensions/sensitive.ts` is being changed by lane safe-search; leave that file
alone and list what step b needs there), the correct command, the Claude Main watchdog, pi RPC.

## Attack (realistic-use bar; build the rows by code, print the table)
States × events: worker in {input, permission, busy tool, queued text, completed, collected, unknown} × {first request, same
prompt seen again, second distinct request before Main polls, resolution, restart of the notifier between open and resolve, two
messages with and without `--resolves`, unconfirmed delivery} × harness {Claude hook path, pi reporter path}. Expected per row:
request files and ids, events, notifications emitted (count and id), request status. Plus the pause rows: Escape succeeds, Escape
sent but worker keeps working, tmux send fails, worker finished meanwhile. Guards: a task with no request files behaves exactly as
today; ordinary messages to a working worker still deliver as now.

## Report
Fail-before against the lane base for each new test. Tests use fake panes in a private tmux server (fresh `TMUX_TMPDIR` plus a
private `-L` socket; never the default server, never kill-server without `-L`) or no tmux at all. The four cells as in round 9 of
SG1 (`/usr/bin/python3` and `/opt/homebrew/bin/python3`, outside tmux with `env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID
-u TP_STATE_DIR` and inside with `TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`), run one after another in the foreground,
each log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`; per-module times; include
tests.test_session_env and every test module that imports what you change (find them); any TS test for register.tsx/reporter.ts.
Report: attack table and counts, sweep of every writer/reader of capability-request.json, pause state and notifications
(file:line), what step b needs, shared files changed, deviations, known limits.
Search with safe_grep and read with Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/`
recursively, name the files; never read `.env*`, auth files or `claude/mcp.json`. Nothing sent to 127.0.0.1:1234 or any network
address; no Claude or pi start. `git add` new files. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last
line, exactly.
