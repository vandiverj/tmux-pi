# T1b: J1 bench modules are outside T1's session-env guard

Lane `test-env-bench`, step T1b. Level: mid coder. Base: `lane/integrate` (68973cb). Touches: `tests/test_bench_*.py`,
`tests/session_env.py`, `tests/test_session_env.py` (tables only).

## Why
T1 (tests import `session_env` first so the caller's TMUX/TP_* never reach code under test) branched before J1's
bench harness (f1c341d) landed. On integrate, `tests.test_session_env` fails 3 tests in both modes:
- `test_every_test_module_runs_the_scrub_import_first`: test_bench_cli, test_bench_runner, test_bench_score and
  test_bench_shell lack `import session_env` as their first statement that runs code.
- `test_a_filtered_discover_of_any_one_module_leaves_no_session_key` (top_level_dir=None): test_bench_cli leaks every key.
- `test_every_session_name_bin_and_lib_read_is_in_session_keys_or_classified_as_configuration`: 7 TP_BENCH_* names
  that lib/bin read are not classified.

## Change
1. Record the 3 failures before any edit (both modes) into `before.log` in your task dir.
2. Add `import session_env` as the first statement that runs code in the 4 bench modules, matching how T1 did the others.
3. Classify each of the 7 TP_BENCH_* names as identity (SESSION_KEYS: it says which session/task/pane this process
   belongs to) or configuration (CONFIG: it tunes behaviour and is safe to inherit). Give one line of evidence per name
   (file:line where it is read and what it means). If a name is identity, check that the bench tests still pass with it scrubbed.
4. Run test_session_env and the 4 bench modules in both modes. Outside mode: run under `env -u` for TMUX, TMUX_PANE and every TP_* key. Inside mode: set dummy TMUX and TP_* values.

## Done means
test_session_env passes in both modes. The bench modules show the same results as before. Each of the 7 names has
evidence. Only the touched paths change. Do not commit. End with a FAILING_TESTS block, or leave it out when nothing
fails, and end with RESULT_STATUS: complete.
