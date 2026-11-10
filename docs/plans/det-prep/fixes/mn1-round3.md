# MN1 round 3: the main id is set up under one lock (Main, 2026-11-09)

Round 2 (coder-7780485606) was checkpoint-committed by Main as ca79210 on lane/main-notify. Main previewed it merged with
main. The full suite discovered from the preview root ran 1237 tests OK in all four cells, and 130 plugin tests passed. The
review is REQUEST_CHANGES with one finding:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-7780485606-175042/result.md
(read it, and `report.md` beside it).

## Fix
1. **F1: two launches race.**
   - In `bin/tp-main`, launch A can read a missing file while launch B creates and exports its id. A then removes B's file
     and installs its own id, so the two mains split.
   - Do every read, check, repair and write of `main-id` under one exclusive lock in the state dir. Use the same kind of
     lock as `claude/plugins/tmux-pi/hooks/report.sh` (`fcntl.flock` through python3) or a `mkdir` lock with a bounded wait.
     Say which you chose and why.
   - Under the lock:
     - read the file;
     - if it holds a valid id, use it, unless a valid `TP_MAIN_ID` was given, which replaces it;
     - otherwise write a fresh id atomically, by a temp file and `mv`;
     - read it back.
   - A valid file is never removed.
   - If the lock cannot be taken within a few seconds, export the id read without repair (if valid), or a fresh id for this
     launch only, and say so on stderr. Never block the launch for long and never export an empty id.
2. **Given id vs. existing file.**
   - Round 2 made a given valid `TP_MAIN_ID` replace the file.
   - Keep that rule and do it under the same lock.
   - Say in the header comment what two concurrent launches with different given ids end with: the last writer wins, and
     each exports its own given id.

## Tests (no live model, no Claude, no pi)
- In `tests/test_main_notify.py`, using the existing stub `tp-claude`:
  - Concurrent launches with no `TP_MAIN_ID` and a missing file (for example 8 at once, started together with a barrier or
    a pipe, as the review did). Every launch exports the same id, and the file holds it.
  - The same with an empty file and with a bad file.
  - The review's reproduction, made deterministic if it can be (for example a hook or env var that pauses between the read
    and the repair, test-only and documented). If it cannot be made deterministic, say why. The concurrent test then runs
    enough rounds to have failed before the fix; show that it did.
  - The existing round-2 tests still pass.
- Fail-before against ca79210. Put the script and log under `evidence/` in the task dir and name them by absolute path.

## Done means
- The modules of round 2's matrix (name them), `tests.test_session_env`, and `claude plugin test claude/plugins/tmux-pi`.
- They pass with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside tmux (`env -u TMUX -u TMUX_PANE -u
  TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
- Rerun them after your last edit, with every new file `git add`-ed, and list each file's time.
- Commit nothing.
- `RESULT_STATUS` on its own line, as the last line of your final message.
- Do not start background shells or watchers that outlive your work. A later turn overwrites the report Main reads.

No live run. Do not start Claude Code or pi, and send nothing to 127.0.0.1:1234. Any test that calls tmux sets
`TMUX_TMPDIR` to a fresh `mktemp -d /tmp/mn1.XXXX` and uses a private `-L` socket; never the default server, and never
`kill-server` without `-L`. Never read `.env*`, and never write that string in a shell command. Never search `bin/` as a
directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively; name the files. Never edit the
Claude profile or its trust store.
