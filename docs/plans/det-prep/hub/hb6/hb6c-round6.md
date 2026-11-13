# HB6c round 6 (mechanical): the same bad record gives the same result on Python 3.9 and 3.14

Main's cells on 1b01882 passed on py3.9. On py3.14 (`/opt/homebrew/bin/python3.14`), both in and out of tmux, one row failed:

    FAIL: test_next_action_table (lane='run', record='t-foreign/state.json', body='[[...[0]]...]')  (1100 deep)
    tests/test_wait_timeline.py:974  self.assertFalse([w for w in listed if Path(w).parent.name in rel], listed)
    AssertionError: [.../tasks/t-foreign/state.json] is not false

The cause: py3.9's json raises RecursionError at that depth, but py3.14 parses it; Main checked depths 5000 and 100000, and both parse. On 3.14, state.json is therefore well-formed JSON that is not an object. Timeline lists it under `problems`. On 3.9, the decoder error takes the per-task isolation path, and the record is not listed.

So **one bad record gets two different `problems` outcomes depending on how it fails**. Fix this in the code, not just in the test.

1. **Rule.** A record that cannot be used is listed under `problems` with its path. That covers a decoder error (RecursionError, UnicodeDecodeError, ValueError), the wrong JSON shape, and a wrong field type. It applies whether or not the task belongs to an open lane, as timeline already does for wrong shapes. The lane outcome is unchanged: a foreign task's bad record never changes another lane's rule. State the rule in report.md with file:line for each path that lists a record. Name any path where a failure is still not listed.
2. **Test.** Change that row (and any sibling row with the same assumption) so its expectation holds on both versions:
   - the record is listed in problems;
   - lane `run` keeps its rule.

   For a decoder error that occurs on every version, keep the 0xFF-byte rows or add one undecodable-bytes row for `state.json`. Do not depend on a nesting depth that only some versions reject. Keep the deep-JSON row: on 3.9 it is a decoder error, on 3.14 a wrong shape, and the expectation is the same.
3. **Run the two modules on both interpreters.** Use `tools/agent/test-run.sh` for each, so there are two runs per module:
   - `/usr/bin/python3` (3.9) and `/opt/homebrew/bin/python3.14`;
   - outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
   - a fresh short `TMUX_TMPDIR` under /tmp;
   - in the foreground.

   Modules: `tests.test_wait_timeline` and `tests.test_session_env`. Report both results.

Touch only tools/waits/status.py and tests/test_wait_timeline.py. Do not touch the default tmux server. Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
