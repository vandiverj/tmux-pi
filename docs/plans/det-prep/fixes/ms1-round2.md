# MS1 round 2: a submit is confirmed only by this message's own record (Main, 2026-11-09)

Round 1 (coder-1396de7de6) was checkpoint-committed by Main as a9b00f8, and Main added `TP_SEND_CONFIRM_SECONDS` to the
session-env CONFIG in f66e831 (both on lane/msg-submit). Your round-1 report was overwritten by a later turn; Main recovered
it to `.../tasks/coder-1396de7de6/main-evidence/worker-report-recovered.md`. The review is REQUEST_CHANGES:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-1396de7de6-164617/result.md
(read it, and `report.md` beside it).

## Fix
1. **F1: the harness comes from the task, not from telemetry.**
   - `lib/pane_send.py` decides "Claude worker" from `state.json` alone. A Claude task whose state is not written yet
     falls back to the pane check, and a loading screen that never showed the text reads as submitted.
   - Take the harness from the task's `meta.json` `worker` field (`claude` or `pi`; every task in this session's state
     dir has one). Use `state.json` `harness` only when `meta.json` has none.
   - A Claude task always uses the submit record. A missing `state.json` means count 0 and no record yet. It never means
     the pane check.
   - The pane check stays for pi only, and for a send with no task dir. Name every caller and say which path each takes.
2. **F2: confirm this delivery, not any increase.**
   - A higher `promptSubmits` alone is not proof. A delayed hook for an earlier prompt can raise the count after the
     snapshot.
   - Confirm only when a submit record after the snapshot carries this message's prompt hash. Keep the hash rule identical
     on both sides, and say what text the hook hashes. Find out whether Claude Code's `UserPromptSubmit` `prompt` is the
     typed text after paste expansion, using the engine types under
     `claude/plugins/tmux-pi/.claude-plugin/types/claude-code/index.d.ts` (name the lines) or a hook-input sample.
   - `report.sh` keeps a bounded list of the last few submit records (count, hash, time), not only the last one, so two
     quick submits cannot hide each other. Keep the existing lock and atomic write.
   - If the same text is sent twice in a row, a record from the first send must not confirm the second. State how the
     snapshot rules that out.
   - If the hook's prompt can differ from the typed text, say so. Then normalise both sides the same way, and test it.
3. **F3: final-tree matrix.** Rerun every selected module after your last edit, with every new file `git add`-ed (the
   session-env guard reads only tracked files). List each file's time.

## Tests (no live model, no Claude, no pi)
- In `tests/test_pane_send.py`:
  - A Claude task (`meta.json` `worker: claude`) with no `state.json` and a pane that never shows the text gives
    `submitted: false`. It fails before the fix.
  - A delayed older record: the count rises after the snapshot with a different prompt hash, and the Enter is ignored.
    The result is `submitted: false`. It fails before the fix.
  - The record carries the right hash, so the result is `submitted: true` with `attempts: 1`.
  - The same text sent twice: the first record does not confirm the second send.
  - A pi task still uses the pane check.
- In `tests/test_claude_report.py`: the submit-record list keeps the last N records in order, and the hash matches the rule
  `lib/pane_send.py` uses.
- Fail-before: each new or changed test against the lane tip f66e831. Put the script and log under `evidence/` in the task
  dir and name them by absolute path.

## Done means
- The round-1 module set plus `tests.test_session_env`.
- They pass with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside tmux (`env -u TMUX -u TMUX_PANE -u
  TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
- Commit nothing.
- `RESULT_STATUS` on its own line, as the last line of your final message.
- Do not start background shells or watchers that outlive your work. A later turn overwrites the report Main reads, and
  that is what happened in round 1.

No live run. Do not start Claude Code or pi, and send nothing to 127.0.0.1:1234. Any test that calls tmux sets
`TMUX_TMPDIR` to a fresh `mktemp -d /tmp/ms1.XXXX` and uses a private `-L` socket; never the default server, and never
`kill-server` without `-L`. Never read `.env*`, and never write that string in a shell command. Never search `bin/` as a
directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively; name the files. Never edit the
Claude profile or its trust store.
