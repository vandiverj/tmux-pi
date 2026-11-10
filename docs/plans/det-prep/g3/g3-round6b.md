# G3 round 6b: the fake worker confirms delivery the way the real hook does (Main, 2026-11-09)

Round 6 is checkpointed as abaf891 on lane/g3-decline and has not been reviewed yet. MS1 has since merged to main (627fdf8):
- `tp-agent message` (through `lib/pane_send.py`) now appends a delivery token ` [tp:<10 hex>]` to the text it types.
- It exits 1 unless a `promptLog` record in the task's `state.json` carries that token. The record is written by
  `claude/plugins/tmux-pi/hooks/report.sh` on `UserPromptSubmit`.

Main previewed lane/g3-decline merged with main 081beb5: `tests.test_g3_e2e` and `tests.test_g3_e2e_tamper` gave 69
tests with failures=68, errors=33. The first failure: `step 'decline': tp-agent message ... exited 1: ... typed but not
confirmed submitted after 3 Enter(s)`. The fake worker in `tools/smoke/g3_e2e.py` never writes what the hook writes.

## Build
1. **Bring main in.** Merge `main` into lane/g3-decline in the worktree first (`git merge main`; resolve nothing by hand
   without saying so). Then work on top.
2. **Fake mode writes what the real hook writes, and nothing more.**
   - When the fake worker receives typed text, it records a `promptLog` entry the same way `report.sh` does on
     `UserPromptSubmit`: the same fields, the same bound and the same token extraction.
   - Read the fields and the bound from `report.sh` and `lib/pane_send.py`; do not guess them. Reuse
     `pane_send.TOKEN_RE` rather than copying the pattern.
   - Better still, where it is practical and stays inside the test cost budget, the fake runs the real `report.sh` with a
     `UserPromptSubmit` event. Say which you chose and why.
3. **The decline step checks the delivery.**
   - `tp-agent message` must report `submitted: true`. Read the field from its JSON output.
   - An unconfirmed send is a failure of step `decline`, with the tool's own words in the error.
   - Add a tamper name for it (for example `unconfirmed-redirect`): the fake drops the record, and the run fails at
     `decline` with that reason.
4. **Live run notes.** In `docs/plans/det-prep/g3/README.md`, say that the live run also checks MS1's open note: the hook's
   `prompt` for a typed (and possibly paste-collapsed) redirect carries the token. A live `submitted: false` with the text
   visibly submitted in the pane is the sign that it does not. Record both the field and the pane tail in the run's facts.
5. Nothing in `lib/` or `bin/` changes. If one looks needed, stop and report it.

## Attack the rule before you report
For the decline step, list the cases from the code. Prior states of the permission request: pending, declined, denied,
authorized, missing. Delivery outcomes: confirmed, unconfirmed, tool error. Orders: redirect recorded before or after the
settle.
- Each case has an expected step outcome and the reason. Run it in fake mode and compare.
- Put the script and log under `evidence/` and report the counts (cases, matches, mismatches fixed).
- Fix any mismatch by the rule. The rows worth keeping go into `tests/test_g3_e2e_tamper.py` or `tests/test_g3_e2e.py`.

## Tests and fail-before
- Fail-before for each new or changed test against abaf891 merged with main (the state Main previewed). Put the script and
  log under `evidence/` and name them by absolute path.
- Every test that starts tmux sets `TMUX_TMPDIR` to a fresh short dir (`mktemp -d /tmp/g3r6b.XXXX`) and uses a private
  `-L` socket. Never touch the default tmux server. Never run `kill-server` without `-L`.

## Done means
- `tests.test_g3_e2e`, `tests.test_g3_e2e_tamper`, `tests.test_placeholder_use`, `tests.test_sensitive_read`,
  `tests.test_pane_send` and `tests.test_session_env` pass with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both
  outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1
  TP_SESSION=dummy TP_TASK_ID=dummy`). Rerun after your last edit and list each file's time in each cell.
- Self-review and SLOW_TESTS. Commit nothing except the merge of main in item 1. `RESULT_STATUS` on its own line, as the
  last line of your final message. No background shells left running.

No live run. Do not start Claude Code or pi, and send nothing to 127.0.0.1:1234. Main runs live run 6 after review. Never
read `.env*`, and never write that string in a shell command, not even in an exclusion glob. Never search `bin/` as a
directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively; name the files.
