# MN1: a worker's state change must reach its main, and a lost notice must never be silent (Main, 2026-11-09)

## What the owner hits
Production issue named by the owner on 2026-11-09: "workers no longer sending messages ack to main agent", and "guessing
it is related to when I am having it do several side tasks that I end up breaking the linking".

## What the code does (production d8a18a7; register.tsx is the same at HEAD)
- The Claude-main notifier is `claude/plugins/tmux-pi/hooks/register.tsx`. On `session.start` it injects the index and then
  polls `tp-agent list --main ID` every 5 s (`poll`). `transitions` marks each new key `delivered` and `wake` calls
  `$.prompt.submit`. It runs only in orchestrator mode (`readGate`, set by `bin/tp-main`). `TP_MAIN=claude` in `bin/tp` is
  the legacy entry and has no notifier. Do not change that here.
- **The link** is `main_id()` in `bin/tp-agent`: `TP_MAIN_ID`, else `CLAUDE_CODE_SESSION_ID`. No launcher sets `TP_MAIN_ID`
  (only lib/trial_session.py does), so in production the link is the Claude session id. `main_rows()` keeps a row of this
  session only when `row.main in (None, ident)`.
- **Reproduced by Main** (fake session dir, two settled tasks in one state dir, `dispatch.json` main `sess-1` and `sess-2`):
  `tp-agent list --main sess-1` returns only the sess-1 task. The other one is dropped with no trace. Any side work that
  runs in another Claude session hides its workers from the main: a second Claude window, `claude -p`, a background or
  forked session, or the main itself after its session id changes.
- **Silent losses in the notifier:**
  - `wake`: after `WAKE_TRIES` failed submits the batch is dropped, yet its keys stay in `delivered`, so the notice is
    never sent again.
  - `runTool`/`runTpAgent` turn a timeout, a non-zero exit or bad JSON into `undefined`, and `poll` skips the tick. If
    `tp-agent list` keeps failing (for example over the 20 s `TP_AGENT_TIMEOUT_MS` with many tasks), main hears nothing
    and nothing says so.

## Build
1. **A stable main identity.**
   - The launcher gives each main a `TP_MAIN_ID` (matching `MAIN_ID`) when it starts. It must survive `/clear`,
     `--resume`/`--continue` and compaction, and be inherited by every process that main starts: the Bash tool, `claude -p`,
     subagents and background shells.
   - Decide where it is made (`bin/tp-main` exports it if unset; consider whether `bin/tp` should set it on the tmux
     session) and say why. A relaunch of the same main in the same tmux session should keep the same id. Persist it in the
     state dir if that is what makes this true, and name the file.
   - Check whether `CLAUDE_CODE_SESSION_ID` actually changes on `/clear` or resume. Use Claude Code's docs (reading sites is
     allowed) or the engine types under `claude/plugins/tmux-pi/types` (name the files). Report what you found and its
     source; do not guess.
   - `main_id()` and `readMainId()` already prefer `TP_MAIN_ID`, so keep that order.
2. **No silent hiding.**
   - `tp-agent list --main ID` also reports the rows of this session it left out because another main's id is on them:
     their count, plus id, status and main for each, in a field the notifier can read.
   - The notifier shows those in the band ("N workers in this session belong to another main: ids…; tp-agent list") and
     toasts once per newly seen row. It does not submit a turn for them, because another live main may own them.
   - Keep rows with no `main` as they are today.
3. **No dropped notices.**
   - A key counts as delivered only once its submit succeeded.
   - A failed submit keeps its rows held and retries on later polls, with a backoff (a named constant), for as long as the
     row is still in that status.
   - After `WAKE_TRIES` failures in a row, toast once that main could not be woken and the band says so, but keep the rows.
4. **No silent poll failure.** Count consecutive failed reads of `tp-agent list`.
   - After 2 in a row, the band shows "worker notices paused: tp-agent list <timed out | exit N | bad output>" and a toast
     is shown once. It clears on the next good read.
   - Keep polling.
5. **Measure `list --main`.**
   - Time it at the lane base with 1, 20 and 60 fake task dirs (half Claude rows), across 1 and 5 session dirs. At HEAD
     `list` also scans tmux panes for trust dialogs per Claude row, so use a private `-L` socket and `TMUX_TMPDIR`.
   - If 60 tasks take over 5 s, fix the cost in this lane (for example scan panes once per call, not once per row).
     Otherwise report the numbers only.
6. **After `/clear`.** Find out from the engine types and the module whether `session.start` fires again on `/clear`, and
   whether the main model then knows about workers it was told of before.
   - If it fires and the model has lost them, re-inject the index on that start, the same way as the first start.
   - If it is not determinable, say so and leave the behaviour unchanged.

## Tests (no live model, no Claude, no pi)
- **Python** (extend `tests/test_main_notify.py`; find the other `list --main` tests by name):
  - The two-main reproduction above: hidden rows are reported in the new field, and the sess-1 row is still listed.
  - `TP_MAIN_ID` set by the launcher is what a child dispatch records. Use a stub `tp-claude` that prints its env, run
    `bin/tp-main` with `TP_MAIN_HARNESS=claude`, and give no real Claude.
  - Two launches of the same main in one session keep one id, if item 1 says they should.
- **Module** (extend `claude/plugins/tmux-pi/hooks/register.test.ts`):
  - A submit that rejects `WAKE_TRIES` times and then succeeds still delivers the row.
  - The row is not lost while main is busy.
  - Poll failures twice in a row put the paused line on the band, and it clears after a good read.
  - Rows owned by another main appear on the band with no submit.
  - The `/clear` behaviour from item 6.
  - Run it the way the existing module tests run (find how in the repo's test docs or package scripts; do not install
    anything).
- **Fail-before:** every new or changed test, run against the lane base. Put the script and log under `evidence/` in the
  task dir and name them by absolute path.

## Done means
1. Items 1 to 6 with tests, and what you found for items 1, 5 and 6 stated with sources.
2. The new tests plus `tests.test_main_notify`, every other test module that covers `tp-agent list`, `bin/tp-main` or
   `bin/tp`, the register module tests, and `tests.test_session_env` pass with `/usr/bin/python3` and
   `/opt/homebrew/bin/python3`, both outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`)
   and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
   - Name the modules you chose and why.
   - Rerun them after the last edit and list each file's time.
3. `RESULT_STATUS` on its own line.

Lane msg-submit (MS1) also edits `bin/tp-agent` (the `message` branch). Keep your `bin/tp-agent` changes to `main_id`,
`main_rows` and `list`, so the merge stays simple.

No live run. Do not start Claude Code or pi, and send nothing to 127.0.0.1:1234. Any test that calls tmux sets `TMUX_TMPDIR`
to a fresh `mktemp -d /tmp/mn1.XXXX` and uses a private `-L` socket; never the default server, never `kill-server` without
`-L`. Never read `.env*`, and never write that string in a shell command. Never search `bin/` as a directory; name the bin
files. Never search `claude/`, `agent/` or `config/` recursively; name the files. Never edit the Claude profile or its trust
store.
