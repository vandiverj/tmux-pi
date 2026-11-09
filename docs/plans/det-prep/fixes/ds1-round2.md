# DS1 round 2 (Main, 2026-11-09)

Round 1 adds `sensitive-read.sh` to the `UserPromptSubmit` and `Stop` groups in `claude/plugins/tmux-pi/hooks/hooks.json`.
That script exits 2 when `bin/tp-sensitive-read` fails for any reason: an import, a JSON parse, the env.
- On `UserPromptSubmit`, exit 2 blocks the owner's prompt and erases it.
- On `Stop`, exit 2 makes Claude continue, which can loop.
- It also starts one more Python process on every turn.

So round 1 adds a fail-closed point to every turn, to settle a record that can safely stay stale.

`redact.sh` (`bin/tp-redact --claude-hook`, then `redact_hook.handle`) is already registered on both events. Its
`Stop`/`UserPromptSubmit` branch runs before any mode check, in `on`, `shadow` and `off` alike, and it never blocks.

## Fix
1. **Settle in `redact_hook.handle`, in every mode.**
   - Remove the `mode() == "on"` condition on `settle_declined` in the turn branch.
   - Remove the `Stop`/`UserPromptSubmit` branch from `bin/tp-sensitive-read`, and its import of `settle_declined` if
     nothing else uses it.
   - Revert the `hooks.json` change entirely.

   This replaces the brief's "exactly one owner per mode" item: there is one hook, `redact.sh`, in every mode. Note that in
   the docstring or comment.
2. **Prove the path is real.** Show that `bin/tp-redact --claude-hook` reaches `handle` for these two events in each mode:
   - with `TP_REDACTION` (or whatever selects the mode) set to `shadow` and to `off`;
   - with any env check that could return before `handle`. Read `bin/tp-redact`. If something returns early in shadow or
     off before the turn branch, say so and fix it narrowly.
3. **Tests.**
   - Change `test_the_worker_hears_a_new_turn_in_both_hooks`. It must now assert that `sensitive-read.sh` is not registered
     on `UserPromptSubmit` or `Stop`, and that `redact.sh` is.
   - Add a test that runs `bin/tp-redact --claude-hook` as a subprocess with a `Stop` event on stdin, in each of the three
     modes. The pending request becomes `declined`, the exit code is 0, and nothing is printed that would block.
   - Add a test that a broken `permission-request.json` (invalid JSON, or a directory) on a `Stop` event still exits 0.
   - Keep the round 1 tests that still apply.
4. **Pi, report only.** Read `agent/extensions/sensitive.ts`, that one named file only; do not search `agent/`.
   - Say whether a declined ask in a Pi worker can leave `permission-request.json` pending, and what would settle it.
   - Give evidence with file:line references. Do not change `agent/`. Main briefs that separately if it is real.
5. **Test placement.** These are product hook tests. If you can move the hook classes out of `tests/test_g3_e2e.py` into a
   hook test file (for example `tests/test_claude_redact_hook.py`) without losing a case, do it; otherwise say why not.

## Done means
- Four cells green for the same 13 files as round 1, plus any file you moved tests to. Each file's time is given.
- Fail-before for the new subprocess test, with round 1's source in place, in mode `shadow` (it must fail there once
  the `tp-sensitive-read` branch is removed and `redact_hook` still checks the mode). Then show it passing.
- The scripts you wrote and ran are listed.
- `RESULT_STATUS` is reported.

Never read `.env*`. No live model. Any test that calls tmux: `TMUX_TMPDIR` set to a temp dir first, and a private `-L` socket.
