# T1 round 2: tests cannot inherit the caller's session, by construction

Review `review-coder-47ae1f2cac-192701` returned REQUEST_CHANGES. Criteria 1–3 and 7 are met; keep that work.

## Findings
- **F1:** the 4 lines in `baseline-failing.txt`. **Main makes this edit at merge** (the file lives only in the main checkout). Nothing to do here; say so in the report.
- **F2:** the parity scan's `USES_HELPER` regex is satisfied by any mention of the helper anywhere in the file, including a comment. A new module can copy `os.environ` into a `bin/` subprocess without isolating, and the scan still passes it.
- **F3:** `test_retry_packet.py` is exempt without the with/without run that criterion 4 requires.
- **N1 (same family, through another route):** `test_lane_writer` (`AliveOnWorkdirTests.test_alive_on_tree_is_alive_on_workdir_with_the_same_answer`) and `test_lane_lease` (`ReservationTests.test_activating_a_pane_that_is_gone_leaves_nothing_alive`) call `lib/worker_process.pane_table` in-process. That reads the inherited `TMUX` and errors when the caller's `TMUX` is not a usable socket. The errors also happen on unmodified `lane/integrate`. A subprocess-only scan cannot see this route.

## Rule (closes the family for every route at once)
**Importing the `tests` package removes the session keys from the test process's own environment, before any test module runs.**
1. Add `tests/__init__.py`. At import it deletes every key in `session_env.SESSION_KEYS` from `os.environ`.
   - It records what it removed in `session_env.CALLER_SESSION`, a dict that tests may read but never put back into `os.environ`.
   - Every way the suite is run (`python3 -m unittest tests.X`, `python3 -m unittest discover -s tests -t .`, a single test id) imports the package first. Verify each one, and document any runner that bypasses it.
2. In-process code (`pane_table`, `tp_agent_inproc`) and every subprocess, whatever env-copy form it uses, then sees no caller session. The F2/F3 problem disappears: there is nothing left to exempt.
3. Keep `outside_session()` and `isolate_session()` as the explicit helpers for tests that build an env on purpose, and keep the 35 conversions. They are harmless and say what they mean.
4. A test that needs a tmux or session value sets it itself, inside the test, with `mock.patch.dict(os.environ, ...)` or an explicit env, for example a private `tmux -L` socket. If any existing test relied on the inherited real `TMUX`, convert it that way and list it.
5. Replace the regex parity and the `EXEMPT` table with runtime checks in `tests/test_session_env.py`:
   - after import, `os.environ` holds none of `SESSION_KEYS`;
   - a child process started with a plain `os.environ.copy()` and a bare `subprocess.run` (no env argument) prints none of the keys;
   - `worker_process.pane_table()`, called in-process while a dummy `TMUX=/tmp/x,1,0` is set in the caller's shell, behaves exactly as it does when `TMUX` is unset. This one is run from a subprocess that launches `python3 -m unittest` with the dummy session env, so the package import is what is under test.
   - Each check must fail with `tests/__init__.py` removed. Show that in the report.
6. `SESSION_KEYS` stays the single list. A parity check compares it against every `os.environ.get("TP_…")`/`environ["…"]` session-identity read in `bin/` and `lib/`, using `git grep` over fixed literal roots. Classify each hit as session identity (in the list) or config (`TP_STATE_DIR`, `TP_WORK_DIR`, `TP_WORKDIR`, …), and put that table in the test.

## Acceptance
- **Criterion 5 ruling (Main):** a converted module passes if it gives the same results with and without the session env. Failures listed in `baseline-failing.txt` that happen identically in both modes are accepted, as long as you name the baseline line.
- Run every module you changed, plus `test_lane_writer`, `test_lane_lease`, `test_retry_packet` (expected: the baseline import error in both modes) and `test_session_env`, separately, twice each:
  - **outside:** all keys unset;
  - **inside:** all keys set to dummy values, including `TMUX=/tmp/x,1,0` (not a socket), `TMUX_PANE=%999`, `TP_TASK_DIR=/nonexistent/dummy`, `TP_MODE=worker`.
  - Report command, count, seconds and outcome for each run. The two N1 tests must now pass inside.
- Paste the per-module table into `result.md` itself; don't point at an earlier report.
