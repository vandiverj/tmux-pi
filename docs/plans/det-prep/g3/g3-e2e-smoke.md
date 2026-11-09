# G3: a repeatable end-to-end smoke script (Main, 2026-11-09)

Release gate G3 (docs/plans/det-prep/release-gates.md, "G3: end-to-end smoke"): one scripted run on the battle-soccer test bed
(`~/Documents/Projects/battle-soccer`) through the real tools. It must exercise these steps, in order:
1. dispatch;
2. a permission prompt that is declined and redirected;
3. retry;
4. cross-provider review;
5. `lane close`;
6. merge;
7. `collect`;
8. `lane remove`.

When it ends, nothing is left over (worktree, tmux session or pane), and the task records, lane records and review verdict are right.
This lane writes the script and its tests. **Main does the live run.** You never start a real worker or reviewer.

## Do
- Read first: `tp-agent --help` and the help for each subcommand above; `lib/lane_commands.py`, `lib/lane_remove.py`, and the files
  that decide task state, including how a waiting permission prompt shows up in a task's state. Read these as named files.
  Also read docs/plans/det-prep/README.md (the battle-soccer description).
- Write `tools/smoke/g3_e2e.py` (Python 3.9 compatible, stdlib only). It drives the real `bin/` tools as subprocesses:
  - Lane: a throwaway lane on a battle-soccer worktree. Name it `g3-smoke-<UTC stamp>`, so runs never collide.
  - Task: a tiny, deterministic coder task with one exact expected diff (for example, add one named constant to one file).
  - Permission prompt: provoke one in a way that does not depend on the model's choice where possible. Decline it the way Main
    does: send Escape, then a redirect message. Find and document the mechanism. If it cannot be made deterministic, say why,
    use the most reliable way you find, and make the script fail clearly (not hang) when no prompt appears in a bounded time.
  - Then retry, review (reviewer provider different from the coder's), close, merge into a scratch integration branch of
    battle-soccer (never its main), collect, and lane remove.
  - After each step, assert the records: task state, lane record, the review verdict file, and the commit on the scratch branch.
  - At the end, assert that no worktree, tmux session, pane or lane record with the run's name is left. Clean up in a `finally`
    block, even after a failure.
  - Write a JSON summary of each step (name, rc, seconds, what was asserted) to a path given by `--out`.
  - Every wait has a bound and a clear error. No unbounded loops.
- Give the script a `--fake` mode in which the model-facing parts (spawn, worker reply, reviewer verdict) are replaced by stubs,
  the same way tests/test_tp_review.py stubs tp-route/tp-spawn/tmux. Write `tests/test_g3_e2e.py` to run the whole flow in
  `--fake` mode on a temp copy of a tiny git repo, not on battle-soccer. Test the leftover checks and the cleanup-after-failure
  path: inject a failure at each step, then check that cleanup ran and the error names the step.
- Fail-before: the test module cannot exist on base. Instead, show that each assertion can fail: for each record check, a fake run
  with that record broken must fail with a message naming it.

## Done means
- `tests/test_g3_e2e.py` passes in four cells: py3.9 and py3.14, each with the session environment set and unset. Each file runs under 60 s.
- A short `docs/plans/det-prep/g3/README.md` with:
  - the exact live command Main will run;
  - what it costs (one coder and one review, both small);
  - the prompt mechanism and how deterministic it is;
  - each assertion.
- State which commands could reach a model. Expected: none in your runs; only the live mode the README names could reach one.
  List every script you wrote and ran.
- End with a line that is exactly `RESULT_STATUS: complete` (or `blocked`).

## Rules
- Never run the live mode, and never start a real worker, reviewer or model from your pane.
- Do not change battle-soccer. The tests use a temp repo.
- Read named files only. Use `git ls-files` to find files. No `rg` or `git grep` over directories. Never read `config/` or `agent/`
  recursively, and never read `.env*`.
- If a product change in `lib/` or `bin/` looks needed, stop and report it with `RESULT_STATUS: blocked`. Do not make it.
