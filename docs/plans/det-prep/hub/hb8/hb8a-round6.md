# HB8a round 6: uncertainty dominates; the grammar accounts for every token (Main, 2026-11-11)

Review review-coder-7405e88297-060917 (REQUEST_CHANGES), report, `probes.py`, `probes.log`, `sweep-rows.json`:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-7405e88297-060917/`.
The HB8a brief and rounds 2-5 still apply. Bar: realistic use. Keep the change local to tools/episodes and its tests.

## Main's diagnosis of round 5 (owner rule: every REQUEST_CHANGES is diagnosed)
- Brief faults (Main's): round 5 did not say that an uncertain call blocks a known *absence* (F1), or that a redirect to a file is a
  write (F3). Rules 1 and 2 below state both.
- Worker misreads of stated rules: the test-run.sh `WT` argument is the record's cwd (F2, round-5 rule 3); heredoc bodies were
  removed before checking for `$( )` (F4, rule 1); a wrapper around test-run.sh lost its provenance (F5, rule 7); ids in tool-result
  and index text were ignored (F6, rule 7 "any notice/index").
- Findings went 12 → 6, narrower families. The approach stands; this round closes it, it does not widen it.

## The rules
1. **Uncertainty dominates.** An uncertain call (round-5 grammar, or rule 2 below) may have run any command and written anything.
   So a measure or check that claims an absence or a count over an interval that holds an uncertain call (no build, no repeated test,
   no message, verification count, stale) is `unknown` with the reason, whether or not the best-effort parse found a matching
   identity. Only a known answer that does not depend on the interval stays known. Closes F1.
2. **Every token is accounted for.** A call is read only if each token of each command is one the grammar models: a redirect to a
   file (`>`, `>>`, `&>`, `2>`, a bare `> f` with no command, `X=1 > f`) is a write at its target (resolved against the stage cwd),
   and `2>&1`/`>/dev/null` are not writes; an unquoted heredoc whose body holds `$`, a backtick or `$( )` makes the call uncertain
   (a quoted `<<'EOF'` body is data); a token the grammar does not model makes the call uncertain, never write-free. Closes F3, F4.
3. **Wrappers keep the record.** `env`, `timeout`, `nice`, `time`, `command` and their combinations around
   `tools/agent/test-run.sh WT … -- CMD` keep `via=test-run.sh` (missing-record warnings included); the record's cwd is `WT` resolved
   against the stage cwd. Two records in different cwds are different checks: a pass in `packages/b` never clears a failure in
   `packages/a`, and they are not repeats of each other. Closes F2 (first half), F5.
4. **A failed cd does not move the cwd.** When the stage's own result shows `cd` failed (exit ≠ 0 of a single-command call, or an
   `&&` chain stopping there), later stages run in the old cwd (or do not run, for `&&`); when it cannot be told, the cwd is unknown.
   Closes F2 (second half).
5. **Request ids from every text that names one.** Include ids found in tool-result text (an index or `tp-agent request` output),
   with the same id pattern the producer uses (`lib/` or `bin/` — name the file you take it from). Closes F6.
- **F7, F8:** known limits, as the review states. **F9 (check):** correct the report: narrow closure claims to what tests show;
  missing run.json is refused, not incomplete-records (say which, and whether that is the right behaviour under round-5 rule 6);
  name the two changed waiting counts in the fake comparison.

## Tests (owner rule since 2026-11-11: tests earn their place)
Extend the existing `GRAMMAR_ROWS` table and the existing rule tests; one table-driven test per rule, not one test per row. Add a row
for every probe in the review's `probes.py`; fail-before against 439f824 per probe. Keep the oracle; add one case per new rule; report
match/mismatch counts. Rerun the 14 fake episodes and compare with round 5 (each new unknown listed with its cause; the good-Main runs
stay known). Report added vs extended tests.

## Verification (owner rule: the worker tests once)
Run `tests.test_episode_harness` and `tests.test_session_env` once with `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`, fresh short `TMUX_TMPDIR`), the log starting with `date -u`,
`TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the full discovery on the preview.
No `--live` runs; no Claude or pi start; private tmux socket only; nothing to 127.0.0.1:1234 or any network. Search with safe_grep,
read with Read/safe_read; never read claude/mcp.json, .env* or auth files. Do not overwrite another task's evidence. Commit nothing.
No background shells. `RESULT_STATUS: complete` as the last line.
