# G1 round 4 (route: design) — the unload must not depend on tp-exit surviving

Rounds 1–3 all failed in family exit-unload. Each round moved the unload past one step that can end the caller:
- round 1: the listing came after the kill;
- round 2: the unload came after the kill;
- round 3: `tp-reset` at `bin/tp-exit:28` kills worker panes before the unload runs.

The tests run tp-exit outside tmux, so they never see the caller die. This round makes the unload independent of the caller, and the test kills the caller.

## F1 — exit-unload (bin/tp-exit)
1. Compute the unload decision first. Move the `local_model.unload_on_exit` block (the `unload_args` construction, including the listing of other sessions and their `@tp_state_dir`) to just after `session` and `state_dir` are known. It must come before the checkpoint, `tp-reset`, `tp-task end-session` and `kill-session`. Keep the round-1 rule: list sessions while this one is still up, and do nothing when the listing fails.
2. Start the unload detached right away, so no later step can stop it:
   `nohup "$ROOT/bin/tp-lmstudio" "${unload_args[@]}" >/dev/null 2>&1 </dev/null &` then `disown`.
   - macOS has no `setsid`. Do not use `tmux run-shell -b`, which dies with the server when the last session closes.
   - The unload uses `--if-idle` with the other sessions' state dirs. This session's workers are about to be stopped, so not waiting for them is correct. Say so in the comment.
3. Remove the old unload call after `tp-task end-session`.

Test (tests/test_lmstudio.py, ExitUnloadTests):
- Add a test whose `tp-reset` stub kills its caller: `kill -KILL $PPID`, where the stub's parent is the tp-exit process.
- Assert that the `lms`/`tp-lmstudio` stub still records `unload --if-idle …`.
- Poll for the record with a bounded loop (at most 5 s, checking every 50 ms). No fixed sleep.
- Keep the existing tests (last session, other sessions' state checked, unload before kill).
- Show the new test fails on the round-3 `bin/tp-exit`: use `git stash` or a copy, and quote the failure.

## F2 — status shows the real TTL (bin/tp-lmstudio)
`lms ps --json` gives `ttlMs` per loaded model. On this Mac Qwen reports `ttlMs: null`, which means it was loaded without a TTL.
- Make `loaded()` also return `ttlMs`.
- In `status`, print the loaded model's TTL: `ttl <n> s`, or `ttl none` when it is null or missing. Print the configured TTL separately, as `configured ttl <n> s`, only if the line needs it.
- Update `README.md:28` if its wording changes.
- Test: an `lms` stub with `ttlMs: null` gives `ttl none`, and one with `ttlMs: 600000` gives `ttl 600 s`.

## Report
- For each module (`tests.test_lmstudio`): the command, the test count, the seconds and the log path (output redirected to `$TP_TASK_DIR/tests.log`).
- A sweep table of every step in `bin/tp-exit` that can end the caller, with the unload's position relative to each.
- Do not commit.
