# MS1: a message to a worker must be submitted, and the sender must know it was (Main, 2026-11-09)

## What happened
G3 live runs 4 and 5 (battle-soccer, Claude Code worker on Haiku) failed at step `decline`. In live run 5, Main captured the
worker pane every 5 s. After Escape, `tp-agent message` typed the redirect into Claude's prompt box, but the message was
never submitted. For the whole 240 s the text sat after `❯`, above "Interrupted · What should Claude do instead?". No
`UserPromptSubmit` fired, so DS1's `settle_declined` never ran. Meanwhile `tp-agent message` had returned `{"sent": true}`
and written `messages.jsonl`.

Cause: `bin/tp-agent` (the `message` branch, about line 1688) sends `send-keys -l TEXT` and then `send-keys Enter` with no
pause. Claude Code reads a fast burst of keys as a paste, so the Enter becomes part of the paste instead of a submit.
`bin/tp-contract` `send()` and `bin/tp-task` (the capability signal) sleep 0.3 s before Enter. tp-agent does not, and none
of the three checks that the message was actually submitted. This is a production defect: any `tp-agent message` to a
Claude worker can silently fail to submit while reporting success.

## Build
1. **One helper**, `lib/pane_send.py`, used by all three senders (tp-agent message, tp-contract `send`, tp-task's signal):
   - Type the text literally, with newlines flattened as today. Wait a settle time (default 0.3 s, a named constant) and
     send Enter.
   - **Confirm the submit.** Prefer the worker's own record over reading the screen. For a Claude worker, the plugin's
     `UserPromptSubmit` hooks already run in the task dir (`claude/plugins/tmux-pi/hooks/report.sh`, `redact.sh`). Make
     report.sh record the prompt submit (for example a `prompt_submitted` time plus a short hash of the prompt in
     state.json, under its existing lock), and wait for a record newer than the send.
     - For a pi worker, check `agent/extensions/reporter.ts` (name the file; do not search `agent/` recursively). If it
       has an equivalent event, use it. If it does not, say so and fall back to the pane check below for pi only.
   - **Retry.** If no confirmation arrives within a bound (named constant, about 3 s), send Enter again, at most 2 more
     times. Never retype the text.
   - **Pane fallback** (only where no record exists): the text is gone from the bottom input region of `capture-pane`.
     Keep it simple and document its limits.
   - Return `submitted` true or false plus the attempts made.
2. **`tp-agent message`**:
   - Print `"submitted": true|false` and the attempts.
   - Write the same fields into the `messages.jsonl` line.
   - When not confirmed, exit non-zero with a message that says what to do. The `messages.jsonl` line is still written
     with `submitted: false`, because the text may be sitting in the box.
   - Keep the `capability-request.json` answer behaviour. Mark the request answered only when the submit is confirmed.
3. **tp-contract `send` and tp-task's signal** use the helper. A failed confirmation in tp-task keeps today's behaviour for
   a failed delivery: with `--approve` the grant is not persisted.
4. **tools/smoke/g3_e2e.py is not in this lane.** It is lane g3-decline (round 6, in progress), which reads messages.jsonl.
   Do not edit it here. In the report, list what g3-decline must change.

## Tests (a fake TUI; no Claude, no pi, no model)
- Write a small fake worker program to run in a private tmux server (`TMUX_TMPDIR` set to a fresh `mktemp -d /tmp/ms1.XXXX`,
  a private `-L` socket; never the default server, never `kill-server` without `-L`).
  - It reads the terminal in raw mode.
  - An Enter that arrives within N ms of the previous key counts as pasted text, not a submit. This models the paste
    detection.
  - On a real submit, it writes the line and fires a record like the hook's.
- Fail-before: against main, an immediate-Enter send leaves the fake unsubmitted. The same test passes with the helper.
- A fake that never confirms gives `submitted: false`, a non-zero exit, the retry count, and a messages.jsonl line with
  `submitted: false`.
- A fake that needs a second Enter is confirmed on the retry, and the text is typed once.
- report.sh writes the submit record on `UserPromptSubmit` and leaves other events alone. Extend the existing report-hook
  tests (find them by name under tests/).
- tp-contract and tp-task use the helper: extend their existing tests. Their fixture tmux stubs (`send-keys) exit 0`) must
  still pass, or be updated with the reason shown.
- Fail-before for each new or changed test against the lane base. Put the script and log under `evidence/` in the task dir
  and name them by absolute path.

## Done means
1. Items 1 to 3 with tests.
2. The new tests plus every existing test module that covers tp-agent message, tp-contract, tp-task, report.sh or the
   plugin hooks, plus `tests.test_session_env`, pass with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside
   tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside
   (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
   - Name the modules you chose and why.
   - Rerun all of them after the last edit and list each file's time.
3. `RESULT_STATUS` on its own line.

No live run. Do not start Claude Code or pi against a model, and send nothing to 127.0.0.1:1234. Never read `.env*`, and
never write that string in a shell command. Never search `bin/` as a directory; name the bin files. Never search `claude/`,
`agent/` or `config/` recursively; name the files.
