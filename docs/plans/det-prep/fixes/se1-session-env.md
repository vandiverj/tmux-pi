# SE1: test_session_env fails on main (Main, 2026-11-09)

`tests/test_session_env.py` is the whole-repo guard against session identity leaking into tests. Two merged lanes (TV1
e1e1b4f, G3 0e88cb9/9263f08) did not run it, and on main it now fails 4 cases (log:
`/Users/jerrickvandiver/.claude/jobs/6061f696/tmp/main-session-env.log`):
1. `test_every_session_name_bin_and_lib_read_is_in_session_keys_or_classified_as_configuration`: `TP_TRUST_CAPTURE_TIMEOUT`
   and `TP_TRUST_SCAN_BUDGET` (lib/trust_dialog.py:22,24) are in neither SESSION_KEYS nor CONFIG.
2. `test_every_test_module_runs_the_scrub_import_first`: tests/test_g3_e2e.py and tests/test_g3_e2e_tamper.py lack
   `import session_env` as their first statement that runs code.
3. `test_a_filtered_discover_of_any_one_module_leaves_no_session_key`: test_g3_e2e_tamper leaks 12 session keys
   (CLAUDE_CODE_SESSION_ID, TMUX, TP_SESSION, ...); also test_lane_writer, which lane AT1 (ambient-tmux) fixes.
4. `test_the_reviewers_repro_passes_under_discover_without_t`: test_lane_writer needs a tmux server; AT1 fixes that.

## Determine first
For 1-3, say why each fails, with evidence: for 1, what each variable is (a test hook or caller identity) from its
readers; for 3, which code in test_g3_e2e_tamper leaves each key (is it only the missing scrub import, or does the module
set keys itself?). For 4, confirm it is only the test_lane_writer case (no other module named in its output).

## Rules
- Classify each variable by what it is: identity goes in SESSION_KEYS, a tuning or test hook goes in CONFIG with a
  comment naming its reader (file:line) like the existing entries. Do not weaken any test in test_session_env.py.
- Lane DS1 also edits tests/test_g3_e2e*.py; touch only the import line(s) there so the merge stays trivial.
- Never kill or touch the default tmux server: every run sets `TMUX_TMPDIR` to a fresh short dir
  (`mktemp -d /tmp/se1.XXXX`), and anything that calls tmux uses a private socket.
- Do not skip, mark expected-failure or delete any test.
- Fail-before: each fixed case fails on the lane base; keep the script and log under `evidence/` in the task dir and
  name every log by absolute path in your report.

## Done means
1. Cases 1-3 fixed with their cause; case 4 (and the test_lane_writer part of 3) shown to be only test_lane_writer.
2. tests.test_session_env, tests.test_g3_e2e and tests.test_g3_e2e_tamper pass in both interpreters
   (`/usr/bin/python3`, `/opt/homebrew/bin/python3`), outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID
   -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`), except for the test_lane_writer
   cases that wait on AT1; each file's time listed.
3. `RESULT_STATUS` reported.

Never read `.env*`, and never write the string `.env` in a shell command, not even in an exclusion glob. Never search `bin/`
as a directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively. No live model.
