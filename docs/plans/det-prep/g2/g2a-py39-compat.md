# G2-A (g2-py39): three Python 3.9 failures in the baseline

Lane `g2-py39`, step G2A. Level: `mid` coder. Base: `main`. Touches: `tests/test_retry_packet.py`,
`tests/run_bounded.py`, `tests/test_run_bounded.py`, `lib/routing_evals.py`, `lib/trial_record.py`,
`tests/test_trial_shadow_record_files.py`, `tests/test_trial_shadow_worker_files.py`,
`docs/plans/det-prep/baseline-failing.txt` (only the lines this lane fixes).

## Why
Release gate G2 (docs/plans/det-prep/release-gates.md): every baseline failure gets a found cause and a real fix;
nothing is skipped, deleted or explained away. Main reproduced these on a clean checkout of main (1b1e9fb), each
test run alone, session keys unset. All three fail only on `/usr/bin/python3` (3.9.6); 3.14 passes.

1. `test_retry_packet` (whole module, `unittest.loader._FailedTest`): the module does not import on 3.9.
   `SyntaxError: f-string expression part cannot include a backslash` at `tests/test_retry_packet.py:176`
   (`review_text`: `{labels or 'F1 [defect] wrong.\nCloses by: code\n'}`). Test-only cause.
2. `test_run_bounded.RunBoundedTests.test_command_line_exit_codes_and_default_log`: the file-mode call
   `run_bounded.py --durations 1 test_tiny.py` exits 2 with empty stderr on 3.9 (test line 232). `unittest --durations`
   exists only from Python 3.12. Find whether `tests/run_bounded.py` should pass `--durations` only where the running
   unittest supports it (and still record per-file times another way on 3.9), or whether the test is wrong; fix the
   real cause so the tool works on 3.9, the interpreter the work Mac uses.
3. `test_trial_shadow_record_files.RecordFileTests.test_the_real_record_builders_read_of_the_task_files`, the four
   `kind='deep'` subtests (meta.json, dispatch.json, state.json, docs-calls.jsonl): `RecursionError: maximum recursion
   depth exceeded while decoding a JSON array` escapes `trial_record.build` via `_json` ->
   `routing_evals._read_json` (`lib/trial_record.py:72-73,106`). The contract (test_trial_shadow_worker_files.py:31,123)
   is that a file nested too deep is treated as malformed, not a crash. Fix where the cause is (the shared reader if
   it should treat over-deep JSON as unreadable for every caller, then check every caller of `_read_json` still does
   the right thing; list them). Explain why 3.14 passes.

## Change
1. Reproduce each first on main under `/usr/bin/python3`; save the before logs in the task dir.
2. Fix each cause. No skips, no version-gated `skipIf`, no deleting subtests.
3. Run, on `/usr/bin/python3` and `/opt/homebrew/bin/python3`, session keys unset and with dummy `TMUX`/`TP_*`:
   test_retry_packet, test_run_bounded, test_trial_shadow_record_files, test_trial_shadow_worker_files, and every
   module that imports `routing_evals` or `trial_record` (list them; say how you found them).
4. Remove from `baseline-failing.txt` exactly the lines your fixes make pass (6 lines), and list them.

Out of scope: other baseline lines; the order-dependent failures (another lane); `lib/redact_hook.py`,
`lib/sensitive_read.py`, `lib/command_wrappers.py` (FG-N1 is open there); `tools/qual/` (LS1 is open there).

## Done means
1. All three pass on both interpreters in both modes; each failed before the fix (logs).
2. Every `_read_json` caller is listed with what it now does on over-deep JSON.
3. The 6 baseline lines are removed and nothing new fails in the modules run.
