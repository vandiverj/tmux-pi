# TV1 (TRUST-VISIBLE): a worker stopped at Claude's folder-trust dialog is reported, never left as "starting"

Lane `trust-visible`, step TV1. Level: `mid` coder. Base: `main`.

Touches:
- `lib/tp_detail.py`, and the inspect/list/wait code paths in `bin/tp-agent`, only where needed;
- `lib/bench/runner.py`: share its detector, do not change its behaviour;
- `tools/smoke/g3_e2e.py` and `docs/plans/det-prep/g3/README.md`;
- tests for each.

Not touched: `config/`, `agent/`, the Claude profile under `~/.config/tmux-pi/claude`.

## Why (Main's G3 live run, 2026-11-09)
Main ran the merged G3 smoke script live on battle-soccer twice.
- Both runs failed at step `prompt`: "no permission prompt ... within 240 s (last seen: starting)". Cleanup was complete
  both times.
- Pane captures at 30, 60, 120 and 200 s show the coder pane sitting at Claude Code's folder-trust screen for the new
  lane worktree ("Quick safety check: Is this a project you created or one you trust? ... No, exit / Yes, I trust this
  folder").
- `tp-agent inspect` reported `starting` the whole time. The dialog shows before any hook loads, so no permission request
  is written.

Folder trust is the owner's decision (`bin/tp-claude-bootstrap`, docs/bench.md step 3). tmux-pi never answers the dialog.
The bug is that it is invisible:
- Main, the G3 script and any orchestrator see "starting" and wait out their full timeout;
- the reason is not named anywhere.

The bench already detects the screen (`lib/bench/runner.py:29` `TRUST_DIALOG`, `trust_dialog_shown`). Task inspection
does not.

## Change
1. **Detect.** For a Claude worker task whose state is `starting` or `working`, check whether its recorded pane shows the
   folder-trust dialog.
   - Use one shared detector, moved out of the bench runner into a small lib module. The bench imports it and keeps its
     behaviour; its tests stay green.
   - Read-only: capture the pane text, never send keys.
   - Bound the tmux call with a timeout. A capture that fails or times out changes nothing; the status stays what it was.
   - Detect only on the dialog's own text. A worker that merely prints the word "trust" must not match: add a negative
     test with such text.
2. **Report.** `tp-agent inspect`, the list/status view and any wait that stops on `needs_input` report
   `status: needs_input` with a detail that:
   - names the folder from the dialog, or the task's workdir;
   - gives the fix: "the owner trusts it once: bin/tp-claude-bootstrap <dir>".

   `inspect --json`, if it exists, carries it as a field, for example `trust_dialog: {dir}`. The worker is never stopped by
   this and the dialog is never answered.
3. **G3 script.**
   - The `prompt` step (and any other wait on the first task) fails at once when inspect reports the trust dialog, with
     "worker stopped at Claude Code's folder-trust dialog for <dir>; the owner runs bin/tp-claude-bootstrap <dir> once,
     then reruns". It does not wait out `--prompt-wait`.
   - The README's live section says the target repository must be trusted in tmux-pi's Claude profile before a live run.
4. **Tests.** Use a tmux shim or a private `-L` server, with `TMUX_TMPDIR` set to a temp dir before the first tmux call:
   - a pane showing the dialog text gives needs_input with the folder and the fix, in inspect and in the list;
   - a pane with ordinary output containing "trust" stays as it was;
   - a hanging capture times out and leaves the status unchanged;
   - the G3 fake flow with a fake trust screen fails step `prompt` at once with the message, and leaves nothing behind;
   - the bench tests are unchanged and green.

## Rules
- Never answer, dismiss or bypass the trust dialog in code or in tests against a real Claude.
- Never edit the Claude profile or its trust store.
- Never read `.env*`.
- Never run the G3 live mode, a live model or a bench run.
- Any test that calls tmux: `TMUX_TMPDIR` set to a temp dir first, and a private `-L` socket.

## Done means
- Four cells green: `/usr/bin/python3` and `/opt/homebrew/bin/python3`, each with the session env set and unset. The cells
  cover the changed modules' tests, tests/test_bench_runner.py, tests/test_g3_e2e.py and tests/test_g3_e2e_tamper.py.
  Each file is under 60 s.
- The tests above, each shown failing before the change.
- The scripts you wrote and ran are listed.
- `RESULT_STATUS` is reported.
