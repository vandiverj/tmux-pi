# G3 round 6: redirect first, then wait for `declined` (Main, 2026-11-09)

Live run 3 (run-log "G3 live run 3") failed at step `decline`. After Escape, Claude Code fires no `PermissionDenied`
for an interactive reject, so `permission-request.json` stayed `pending` and the script waited 240 s for it to settle
before sending the redirect. DS1 (merged 9915fd8) added `lib/redact_hook.py:settle_declined`. The request is now
settled `declined` (not `denied`, which only `PermissionDenied` writes) by the asker's next `UserPromptSubmit` or `Stop`,
and every other field is kept. The redirect message is that next prompt, so the script must send it first.

## Change
1. **`step_decline`** in `tools/smoke/g3_e2e.py`:
   - Send `Escape`.
   - Send the redirect with `tp-agent message` and check that `messages.jsonl` holds it.
   - Wait for the request to leave `pending`.
   - Check that the status is `declined` or `denied`, never `authorized`, and record which one in the run's facts or
     summary. A `declined` request must also carry the settling event (`UserPromptSubmit` or `Stop`) as written by
     `settled_record`. Read the field names from `lib/redact_hook.py`; do not guess them.
   - Keep the tamper names and their steps (`still-pending`, `authorized`, `no-redirect-record`, ...). Fake mode writes
     what the real hook writes: `declined` with the settling event, and only after the redirect is recorded.
2. **Ordering is part of the contract.** The script never waits for the settle before the redirect is recorded. Add a
   fake-mode test that fails if it does: the fake settles the request only after the redirect is in `messages.jsonl`.
   The old order must then time out at `decline`, with a small wait.
3. **`HookTriggerTest`** (`tests/test_g3_e2e.py`): keep the `PermissionDenied` case, which still settles `denied`. Add the
   live path:
   - the real `redact_hook.handle` with a `UserPromptSubmit` event (no `agent_id`) in each `TP_REDACTION` mode settles
     the pending request to `declined` and keeps its other fields;
   - `hooks.json` registers `redact.sh` on `UserPromptSubmit`, `Stop` and `SubagentStop`.
   Build strings from the script's constants as today; no shell command names a dotenv-shaped path.
4. **`docs/plans/det-prep/g3/README.md`**: replace the claim that "the `PermissionDenied` event settles the request to
   `denied`". Say what happens instead: no event on an interactive reject, and the redirect's `UserPromptSubmit` settles it
   `declined` (DS1). Update the decline and tamper lines to the new order. Check the rest of the README for the same claim.
5. Nothing in `lib/` or `bin/` changes. If a lib change looks needed, stop and report it.

## Tests and fail-before
- Each new or changed test fails on the lane base (main 13189d4). Keep the script and log under `evidence/` in the task
  dir, and name every log by absolute path.
- Every test that starts tmux sets `TMUX_TMPDIR` to a fresh short dir (`mktemp -d /tmp/g3r6.XXXX`) and uses a private
  `-L` socket. Never touch the default tmux server. Never run `kill-server` without `-L`.
- No live run. Do not start Claude Code or pi, and do not send any request to 127.0.0.1:1234. Main runs live run 4 after
  merge.

## Done means
1. Items 1 to 4, with the tests.
2. `tests.test_g3_e2e`, `tests.test_g3_e2e_tamper`, `tests.test_placeholder_use`, `tests.test_sensitive_read` and
   `tests.test_session_env` pass with both interpreters (`/usr/bin/python3`, `/opt/homebrew/bin/python3`), in both modes:
   - outside tmux: `env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`;
   - inside tmux: `TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`.

   List each file's time in each cell.
3. `RESULT_STATUS` reported on its own line.

Never read `.env*`, and never write the string `.env` in a shell command, not even in an exclusion glob. Never search
`bin/` as a directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively; name the files.
