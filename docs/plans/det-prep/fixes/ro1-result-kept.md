# RO1: a worker's report must not be replaced by a later, unprompted turn (Main, 2026-11-09)

## What happened
MS1 worker coder-1396de7de6 wrote its full report (7530 chars, ending `RESULT_STATUS: complete`) at the end of its turn.
- About 45 minutes later, three Claude Code `<task-notification>` turns arrived (22:02:58Z, 22:03:36Z, 22:03:39Z). They
  came from background shells the worker itself had started.
- Each one started a turn, and each turn ended with a short reply ("That is another log-watcher finishing, with nothing new
  in it…").
- `result.md` now holds only that short reply. The real report survives only in the transcript.
- Main reads `result.md`, so a main sees "nothing new" in place of the report.

This is in production: `claude/plugins/tmux-pi/hooks/report.sh` is unchanged since d8a18a7. The owner reported "workers no
longer sending messages ack to main agent", and their main uses subagents and background work heavily. This is one way that
report goes missing.

## Cause
`report.sh`:
- On `UserPromptSubmit` it removes `result.md`.
- On a `Stop` with an open prompt it writes `last_assistant_message` to `result.md`.
- Any new turn replaces the report: a main's message, a task notification, or a hook-driven turn alike. The settled status
  goes with it.

## Build
1. **Keep every report.** Before `result.md` is removed or replaced, copy it to `results/<turn>.md` in the task dir. Use
   the turn counter `report.sh` already keeps (`promptTurn`). Keep the existing lock and atomic writes.
2. **An unprompted turn does not replace a final report.**
   - Find out from Claude Code's own records what starts such a turn. Does a task notification fire `UserPromptSubmit`?
     What is its prompt text? Use the engine types under `claude/plugins/tmux-pi/.claude-plugin/types/claude-code/index.d.ts`
     (name the lines) and a hook-input sample if the types are silent. Reading sites is allowed.
   - Then make this rule hold:
     - A turn whose reply has no `RESULT_STATUS:` line does not replace a `result.md` that has one, unless the turn was
       started by a prompt from outside the worker: a `tp-agent message`, a retry, or a capability answer.
     - Its reply goes to `results/<turn>.md`.
     - `state.json` records that a later reply exists (for example `laterReplies: N` and the latest path).
   - If the start of the turn cannot be told apart from the hook input, use this simpler rule instead and say why: a reply
     without `RESULT_STATUS:` never replaces one with it.
   - **Main's lead (2026-11-09).** The transcript tells the turns apart even if the hook input does not. In the MS1
     worker's transcript (path in `.../tasks/coder-1396de7de6/transcript-path`), each user entry has `promptSource`,
     `turnOrigin` and `origin.kind`:
     - the dispatch prompt: `"typed"`, `"human"`, `{"kind": "human"}`;
     - the three task-notification turns: `"system"`, `"task_notification"`, `{"kind": "task-notification", "producer":
       "session-task", …}`.
     `report.sh` already reads `transcript_path`. Check whether the hook input carries any of these fields. If it does
     not, read the newest user entry in the transcript at `Stop`. Handle a transcript that is not flushed yet as "origin
     unknown", which falls back to the simpler rule.
3. **Main sees both.**
   - `tp-agent inspect` and `tp-agent list` show that a later reply exists, so a main knows to look.
   - The notifier (`register.tsx`) is not changed in this lane.
4. **The packet tells workers not to leave background watchers running after their report.** Find where the worker packet
   text is built (by name in `bin/tp-agent` or `lib/`) and add one sentence. This is a mitigation; item 2 is the fix.

## Tests (no live model, no Claude)
- Extend the existing report-hook tests (find them by name, for example `tests/test_claude_report.py`):
  - A Stop with a full `RESULT_STATUS` report, then a turn that is a task notification and ends with a short reply. The
    report stays in `result.md`, the short reply is in `results/`, and `state.json` says a later reply exists.
  - A `tp-agent message` turn whose reply has `RESULT_STATUS` replaces `result.md`, and the old report is in `results/`.
  - Duplicate or late `Stop` behaviour is unchanged (the existing tests still pass).
- Fail-before for each new or changed test against the lane base. Put the script and log under `evidence/` in the task dir
  and name them by absolute path.

## Done means
- The new tests plus every module that covers `report.sh`, `tp-agent inspect`/`list` or the worker packet text (name them),
  plus `tests.test_session_env`.
- They pass with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside tmux (`env -u TMUX -u TMUX_PANE -u
  TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
- Commit nothing, but run the matrix with every new file `git add`-ed (the session-env guard reads only tracked files).
- Rerun after your last edit and list each file's time.
- `RESULT_STATUS` on its own line, as the last line of your final message. Do not start background watchers that outlive
  your work.

No live run. Do not start Claude Code or pi, and send nothing to 127.0.0.1:1234. Any test that calls tmux sets `TMUX_TMPDIR`
to a fresh `mktemp -d /tmp/ro1.XXXX` and uses a private `-L` socket; never the default server, never `kill-server` without
`-L`. Never read `.env*`, and never write that string in a shell command. Never search `bin/` as a directory; name the bin
files. Never search `claude/`, `agent/` or `config/` recursively; name the files. Never edit the Claude profile or its trust
store.
