# T1: tests don't inherit the caller's tmux-pi session

Lane `test-env`, step t1. Base: lane/integrate.
- Level: small. Test-only change; product code is not touched.
- Touches: `tests/` only (a new shared helper, the modules it converts, and a parity test), plus `docs/plans/det-prep/baseline-failing.txt`.

## Why
Four baseline entries in `tests/test_orchestration.py` fail only when the suite is run from inside a Claude Code or tmux-pi session:
- `test_every_reader_refuses_every_invalid_queue_shape`, the bare test and its two `main missing` subtests;
- `test_readers_cannot_be_handed_an_unchecked_queue_item`.

The cause:
- The fixture env at `tests/test_orchestration.py:40` is `{**os.environ, ...}` and pops only `TMUX`.
- `bin/tp-agent` `main_id()` (around `:290-297`) reads `TP_MAIN_ID` or `CLAUDE_CODE_SESSION_ID`. Since commit `03065fe`, `main` is optional and is filled from that env (`:785-788`).
- So a queue record that is meant to be invalid ("main missing") gets a main id from the caller's session, and it is accepted.
- One test also loads `bin/tp-agent` in-process, so the in-process `os.environ` leaks too, not only the subprocess env.

Measured on 2026-11-07 in lane/integrate with `CLAUDE_CODE_SESSION_ID`, `TP_MAIN_ID` and `TP_TASK_DIR` unset: `test_orchestration` passes all 40 tests. With them set, those 4 fail. No other baseline entry changed. `tests/test_main_notify.py:31` already strips these keys by hand, so the fix exists in one place but not as a rule.

## Rule (closes the family)
A test's subprocess env, and its in-process `os.environ` when it loads a `bin/` script, never carries the session of whoever runs the suite. One shared helper defines the session keys, and every fixture uses it:
1. Add `tests/session_env.py` with:
   - `SESSION_KEYS`: `TMUX`, `TMUX_PANE`, `TP_TASK_ID`, `TP_TASK_DIR`, `TP_MAIN_ID`, `TP_MAIN_CWD`, `TP_TRIAL_ID`, `CLAUDE_CODE_SESSION_ID`. Check `bin/` and `lib/` for any other `TP_*` key that identifies the *caller's* session (not configuration such as `TP_STATE_DIR`), add it, and list what you checked in the report.
   - `outside_session(env=None)`, which returns a copy of `env` (default `os.environ`) without those keys.
   - `isolate_session(testcase)`, which applies `mock.patch.dict(os.environ)` to the test, removes the keys, and registers the undo with `addCleanup`.
2. `tests/test_orchestration.py`: build `self.env` from `outside_session()` and call `isolate_session(self)` in `setUp`.
3. `tests/test_main_notify.py:31`: replace the hand-written key list with `outside_session()`.
4. A test that wants a session id sets it explicitly, as `test_main_notify` already does.

## Tests
- A positive case in `test_orchestration`: a dispatch with `TP_MAIN_ID` set explicitly in the fixture env records that main. The "main missing" refusal still holds when it is unset. This shows the optional-main path from `03065fe` still works.
- Add the parity test `tests/test_session_env.py`. It scans `tests/*.py` for every module that both runs a `bin/` script (subprocess or in-process load) and copies `os.environ` (`{**os.environ`, `os.environ.copy()`, `dict(os.environ)`). Each such module must either call `outside_session`/`isolate_session` or be listed in an `EXEMPT` table in the parity test with a one-line reason, and each exemption is a module you showed gives the same results with and without the session keys. The parity test fails when a new module copies `os.environ` without either.
- Convert the modules the scan finds. Converting is preferred over exempting. Keep each conversion to the env line.
- Run `tests/test_orchestration.py` twice, with the session keys set to dummy values (`CLAUDE_CODE_SESSION_ID=sess-x TP_MAIN_ID=main-x`) and with them unset; it must pass both ways. Do the same for every module you converted.

## Done means
1. `tests/session_env.py` exists, and `test_orchestration` and `test_main_notify` use it.
2. `python3 -m unittest tests.test_orchestration` passes with the session keys set to dummy values and with them unset. Both runs are in the report with counts and seconds.
3. The positive optional-main test exists and passes.
4. The parity test exists and passes. Its EXEMPT table is empty or each row has a reason and a with/without run.
5. Each converted module passes both ways. Report every module, its command, its count and its seconds.
6. `docs/plans/det-prep/baseline-failing.txt` loses exactly the four `test_orchestration` lines named above and nothing else.
7. No file outside `tests/` and that baseline file changes.
