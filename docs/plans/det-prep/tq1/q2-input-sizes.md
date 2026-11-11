# TQ1 Q2: tests that repeat or oversize their expensive rows (Main, 2026-11-11)

Owner decision: "all as recommended" (docs/plans/det-prep/tq1/decisions.md). Reports: S2-sensitive.md and S4-summarizer-g3.md in
this directory. Kind of round: mechanical. Change tests only; no production code change. Every assertion is kept.

## The three changes
1. **S2(a) the sensitive-read reruns.** `tests/test_sensitive_trust.py:194-198` re-runs every test of
   `test_sensitive_read.SensitiveReadTableTests` (and `UnscopedPatchTests`) with an unrelated and with a refused trust record, to
   prove a record that does not apply changes no answer. Keep that proof for the decision rows (in-process, fast). For the two
   expensive rows, `test_oversized_input_is_judged_without_failing` and
   `test_claude_hook_refuses_a_worker_shell_search_and_names_the_safe_tool`, the plain run stays whole; in each rerun one
   representative input of each is checked. Show by reading the code that the trust lookup is the same for every input of those
   rows (name the function and line) before cutting.
2. **S2(b) oversized inputs.** The oversized test uses 150k-250k character commands. Find whether any limit in
   `lib/sensitive_read.py` (or what it calls) needs that size to be crossed (the report found `_brace_expand` caps at 64
   alternatives and walks at 2 s; no size limit yet). If none: shrink to the smallest size that still reaches the same lines and
   still fails on the bug the test was added for (`git log -S`/`-L` on the test names the commit; restore that code in a scratch
   worktree, show the test fails at the new size, revert). If a limit exists, keep each input just above it.
3. **S4(b) summarizer oracle and linear-time inputs.** The three `tests/test_qual_summarize_oracle_characters_*` modules (a
   generated case list per delimiter family) and `tests/test_qual_summarize_linear.py` (megabyte inputs; bounded by step count
   since eb8661a). For each: find the smallest size/case list that still fails on the bug it was written for (same scratch
   restore as above) and shrink to it. Keep the three-module split (it exists to run in parallel, 9889bd4). Every delimiter family
   keeps at least the cases that reach each distinct branch; say which you cut and why.

## Proof (in the report)
Per test: time and count before and after (one run each, outside tmux); the fail-on-old-bug runs (commit, scratch restore, the
failing output at the new size, revert); for (1), the rows kept in the reruns. Main measures coverage on the preview.

## Verification (the worker tests once)
Run tests.test_sensitive_trust, tests.test_sensitive_read, tests.test_qual_summarize_oracle, the three
tests.test_qual_summarize_oracle_characters_* modules, tests.test_qual_summarize_linear, tests.test_session_env once with
`/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`), short fresh
`TMUX_TMPDIR` under /tmp, `perl -e 'alarm 900; exec @ARGV'`; the log starts with `date -u`, `TMUX_TMPDIR` and
`git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the full discovery. Nothing to 127.0.0.1:1234 or
any network; no Claude or pi start; private tmux socket only. Do not edit config/. Search with safe_grep, read with Read/safe_read;
never read claude/mcp.json, .env*, auth files or config/local.json. Commit nothing. No background shells.
`RESULT_STATUS: complete` as the last line.
