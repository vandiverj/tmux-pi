# HB2b round 2: a launch record never stops a launch (Main, 2026-11-11)

The HB2b brief (`hb2b-brief.md`, this dir) still applies. Kind of round: mechanical (Sonnet). Lane base for fail-before: a200f4f.

## What Main found
Main's four cells on main 1dff1e0 + a200f4f: `tests.test_worker_env` PreambleArgsTests `test_the_preamble_gets_state_dir_and_parent`
and `test_a_task_without_a_parent_passes_neither` fail in all four (pass on main). That test's tree has a stub
`lib/preamble.py` without `contract`; `lib/launch_record.py:204` raises AttributeError, `_main` exits 1, and tp-spawn stops
the launch (returncode 1). **Diagnosis:** brief's fault for not naming every test module that runs tp-spawn; the worker's for
letting the record decide whether a worker starts.

## The rules
1. **The record never stops a launch.** Any error while building or writing `launch.json` (or `main-launch.json`) leaves the
   launch exactly as it would be without HB2b. The record is then written as `{"schema": "tp-launch:v1", "status": "error",
   "error": "<exception type and message>", ...}` with whatever fields are known (atomic, same path) or, if even that fails, one
   line to stderr; never a traceback that changes the exit code. A test proves it (the stub-preamble case is the realistic one:
   a copied tree with an older lib).
2. **Test trees that copy lib files.** Where a test copies a fixed list of lib files into a tree that runs tp-spawn, add
   `launch_record.py` (and what it imports) only if the test checks the record; otherwise rule 1 covers it. Do not weaken
   test_worker_env's assertions.
3. Every test module that runs tp-spawn or tp-main or imports preamble/launch_record runs once (list below).

## Verification (the worker tests once)
Run once, `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u
TP_STATE_DIR`) through `tools/agent/test-run.sh`, fresh short `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 900; exec @ARGV'`,
log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`: tests.test_autonomy tests.test_claude_launcher_settings tests.test_claude_redact_hook tests.test_claude_subagents_mcp tests.test_context_broker tests.test_contract tests.test_decisions_gate tests.test_dispatcher_runtime tests.test_episode_harness tests.test_goal tests.test_hub_records tests.test_lane_index tests.test_lane_lease tests.test_lane_other_paths tests.test_lane_recovery tests.test_lane_remove tests.test_lane_steps tests.test_lane_writer tests.test_launcher_workdir tests.test_lmstudio tests.test_local_provider tests.test_main_notify tests.test_orchestration tests.test_pi_safe_tools tests.test_preamble tests.test_process_end tests.test_qual_summarize tests.test_qual_summarize_oracle tests.test_refusal_remedies tests.test_reporter_symlinks tests.test_review_recovery tests.test_sensitive_read tests.test_sensitive_trust tests.test_session_resolution tests.test_state_symlinks tests.test_tp_review tests.test_trial_session tests.test_trial_shadow tests.test_trial_shadow_gate tests.test_trial_shadow_owner tests.test_trial_shadow_records tests.test_trial_shadow_run tests.test_trial_shadow_worker_files tests.test_worker_env tests.test_worker_workdir  tests.test_session_env.
Split into runs under 10 minutes each; run them one after another in the foreground (no background shells). Main runs the cells
and discovery. No Claude or pi start; nothing to 127.0.0.1:1234 or any network; never the default tmux server (not even
read-only: use a fake tmux or a private `-L` socket). Edit only the files HB2b touched plus test files that copy lib lists.
Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. If a guard
hook refuses a command, stop and report it; do not work around it. Do not approve any permission or placeholder prompt. Commit
nothing. `RESULT_STATUS: complete` as the last line.
