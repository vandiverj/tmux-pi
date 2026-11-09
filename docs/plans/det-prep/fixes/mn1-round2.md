# MN1 round 2: the stable main id must hold in every path (Main, 2026-11-09)

Round 1 (coder-61e301dd2b) was checkpoint-committed by Main as 2149806 on lane/main-notify. Main previewed it merged with
main: 280 tests OK in all four cells, and 130 module tests passed. The review is REQUEST_CHANGES with three findings, all in
`bin/tp-main` and the launch path:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-61e301dd2b-155936/result.md
(read it, and `report.md` beside it).

The owner has since confirmed that their production main is Claude Code and that "side tasks" are subagents. Main checked
Claude Code 2.1.321: a subagent's Bash tool sees the main's own `CLAUDE_CODE_SESSION_ID`. So the session id the link breaks on
is the one that changes on `/clear` and resume. The stable id is what fixes it, and it must not fail quietly.

## Do
1. **F3: an empty or bad `main-id` file.** A `main-id` file that is empty or does not match the id pattern is replaced with a
   fresh id. The id `tp-main` exports must always match the pattern. If the file still cannot be read back, export the fresh
   id and say so on stderr. Never export an empty `TP_MAIN_ID`.
2. **F2: an id given at launch.**
   - A valid `TP_MAIN_ID` set when `tp-main` starts is written to `main-id` when the state dir exists, replacing what was
     there. A later launch without it then keeps the same id.
   - First check every setter of `TP_MAIN_ID` (`lib/trial_session.py` and anything you find by name in tests/ and lib/). Make
     sure none of them would overwrite the `main-id` of a real main's state dir, and say what you found.
3. **F1: tmux workers.** A worker started by `tp-agent dispatch` must carry the dispatching main's `TP_MAIN_ID`, so tasks
   it dispatches in turn are tagged to the same main.
   - Pass it from the dispatcher's environment through `bin/tp-spawn` into the worker's `run.sh` env, only when it is set and
     valid.
   - Do not put it in the tmux session environment in `bin/tp`: two mains in one tmux session would then share one id.
   - Check that a worker with `TP_MAIN_ID` set does not start the notifier (`readGate` in register.tsx needs orchestrator
     mode), and say how you checked.

## Tests (no live model, no Claude, no pi)
- `tests/test_main_notify.py`, using the existing stub `tp-claude`:
  - An empty `main-id`, and one holding a bad id, each give a valid fresh id that a second launch reads back.
  - A launch with `TP_MAIN_ID=main-given`, then a launch without it in the same state dir, both export `main-given`.
  - A dispatch from an environment with `TP_MAIN_ID` set gives a worker `run.sh` that exports it, and a dispatch without it
    gives none. Find the existing tp-spawn or dispatch tests by name and extend one if it fits.
- Fail-before: each new or changed test against 2149806. Put the script and log under `evidence/` in the task dir and name
  them by absolute path.

## Done means
- The modules of round 1's matrix, plus every test module that covers `bin/tp-spawn` (name them), and `claude plugin test
  claude/plugins/tmux-pi`.
- They pass with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside tmux (`env -u TMUX -u TMUX_PANE -u
  TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
- Rerun them after your last edit and list each file's time.
- `RESULT_STATUS` on its own line, as the last line of your final message.
- Do not start background watchers that outlive your work. A later turn overwrites the report main reads.

Lane msg-submit (MS1) also edits `bin/tp-agent`. Keep your `bin/tp-agent` changes, if any, to `main_id`, `main_rows`, `list`
and dispatch's env.

No live run. Do not start Claude Code or pi, and send nothing to 127.0.0.1:1234. Any test that calls tmux sets `TMUX_TMPDIR`
to a fresh `mktemp -d /tmp/mn1.XXXX` and uses a private `-L` socket; never the default server, never `kill-server` without
`-L`. Never read `.env*`, and never write that string in a shell command. Never search `bin/` as a directory; name the bin
files. Never search `claude/`, `agent/` or `config/` recursively; name the files. Never edit the Claude profile or its trust
store.
