# F-GATE-TS: one gate record, written once (Main, 2026-11-08)

Lane `gate-record-once`, step F1. Found by Main's full-suite matrix on the G2-F worktree (py3.9, out of session, 4678 tests):

```
FAIL: test_the_record_has_a_status_per_check_the_tree_and_goes_beside_the_log_and_to_the_task_dir (test_agent_tools.GateTests)
  File "tests/test_agent_tools.py", line 370
    self.assertEqual((task / "gate.json").read_text(), beside.read_text())
AssertionError: '{"sc[829 chars]:47:21Z", "log": ...' != '{"sc[829 chars]:47:20Z", "log": ...'
```

## Cause (Main's reading; confirm or correct it)
`tools/agent/pre-commit-gate.sh:411-418` `write_record` calls `$(tp_now)` for `finished` each time it runs. It runs twice: once beside
the log (:422) and once to `TP_TASK_DIR/gate.json` (:430). When the two writes fall on different seconds, the two copies of one
record differ. This is a product defect, not only a test flake: a reader of either copy sees a different record.

## Rules
1. **Prove the tie first.** Write a deterministic reproducer that makes the two writes fall on different seconds. Do not sleep and
   hope. For example, put a `tp_now` stub on PATH or in the environment that returns a different value on each call, if
   `_common.sh` allows that. Otherwise drive it another way and say how. It must fail on main before the fix, every time.
2. **Fix in the product.** Compute every field of the record once, before either write. The two files are byte-identical by
   construction. Prefer writing the record once and copying it, or one token list written twice; say which you chose and why.
3. **Sweep.** Find every other place in `tools/agent/` and `bin/`/`lib/` where one record or result is written to more than one place.
   Check whether any of them reads the clock, a random value or a counter per write. Use a table: site | what is written twice |
   reads per write? | fixed or why safe. Fix any site of the same kind in this lane; list anything else for Main.
4. **Tests.** The reproducer from rule 1 becomes a test in `tests/test_agent_tools.py` GateTests. Keep the existing test as it is.
   Repeat GateTests 30 times on both interpreters and report the count.

## Verification and claims
- Modules: test_agent_tools, test_gate_record (if present), plus any module that imports what you change. Both interpreters, in and
  out of session (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy` vs those unset).
- No live gate runs against a real repository other than the test fixtures.
- List every script you wrote and ran, by path in the task directory.
- End with a line that is exactly `RESULT_STATUS: complete` (or `blocked`).

Touches: tools/agent/pre-commit-gate.sh, tools/agent/_common.sh (only if needed for the reproducer or a shared fix), tests/test_agent_tools.py.
