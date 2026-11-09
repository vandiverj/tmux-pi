# LS1b round 3 (Main, 2026-11-09)

Main ran the round 2 worktree in four cells, over test_qual_summarize, test_qual_summarize_linear,
test_qual_summarize_oracle, test_router_catalog and test_route_resolve.
- The round 2 work is green.
- `tests/test_qual_summarize_oracle.py` fails in every cell with 21 errors (`PathOracleTests`, every `test_generated_*`).

The error is `ValueError: substring not found` at `oracle_cases`, `dot = target.rindex(".")` (line 200).
- The generator assumes every cited target has a file extension.
- Round 1 added two real chores whose first citation is `real/bin/tp-spawn`, which has none: `real-spawn-lease-calls`
  and `real-spawn-lanes-py`.

Round 2's report said the oracle module did not need a rerun. That was wrong: the oracle iterates over every answer,
including the new split. From now on, run every `tests/test_qual_summarize*.py` module in every cell.

## Fix
1. **The oracle covers extensionless targets.** Extensionless paths are real; bin/ scripts are the common case on real
   work.
   - Do not skip these chores, and do not special-case the two ids.
   - Make the generator correct for a target with no `.` in its last path segment. Also handle a `.` that is only in a
     directory name.
   - Every family (characters, separators, prefixes, leadins, bare) must still produce cases for these targets. Where a
     place only makes sense for a target with an extension ("before the extension"), state what replaces it, or that it is
     dropped, and why, in a comment and in the report.
2. **Any mismatch is a checker question.** If the oracle and `tools/qual/summarize_check.py` disagree on an extensionless
   citation, decide which is right from the rules in the checker's docstring.
   - If the checker is wrong, fix it, with a test that fails before the fix.
   - The checker must still return the same result for every existing synthetic case: the existing oracle and
     differential tests stay green.
   - Run the linear-time step-count tests again after any checker change, and update the budgets only if the counts
     changed, with the reason.
3. **A guard for the class.** Add a test that every `cite_files` entry in answers.jsonl, across all splits, is a path the
   oracle and the checker both handle. It must fail with the old `rindex` line in place.
4. **Budget.** Each test file under 60 s in all four cells. If the oracle file goes over with 36 chores, split it without
   dropping a case.

## Done means
- four cells green for all of `tests/test_qual_summarize*.py`, plus test_router_catalog and test_route_resolve, with each
  file's time;
- the fail-before for item 3;
- the scripts you wrote and ran are listed;
- `RESULT_STATUS` is reported.

No tmux, no live model, never read `.env*`. Keep all round 1 and round 2 work.
