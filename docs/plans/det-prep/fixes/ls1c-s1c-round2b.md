# LS1c S1c round 2b: integrate with main (Main, 2026-11-09)

Round 2 (coder-6a357383f2) was checkpoint-committed by Main as 4529d7c on lane/code-facts. Main previewed it merged with main.
All four cells fail one test, the same way in each:

```
ERROR: test_calibration_lines_cite_their_line (tests.test_qual_summarize_code_facts.GuardTests)
  files = ss.prepare(root, row["files"])
summarize_structured.PrepareError: files total 122728 chars, over the budget of 60000: real/lib/lanes.py 63857 chars, ...
```

Cause: main merged LS1b (db29594) after this lane's base. That merge added the `real` split rows to
`tests/calibration/summarize/chores.jsonl`, and some of them are over the default prepare budget. The guard iterates every row.

## Do
1. In the lane worktree, run `git merge main`. It should be clean; if not, stop and report.
2. Make the guard cover every row, the real split included. `code_facts` is computed by code and has no model budget, so
   prepare each row with a budget large enough for the biggest row (a named constant in the test, with the reason). Do not
   skip rows and do not filter by split.
3. If any real-split diff or use line then fails its citation check, that is a defect in `code_facts`. Fix the code, not the
   test, and add a focused test for it.
4. Report how many lines the guard checked per split, before and after.

## Done means
The same modules and matrix as round 2 (docs/plans/det-prep/fixes/ls1c-s1c-round2.md, "Done means" item 2), on the merged
tree. Rerun after the last edit, list each file's time, and give a fail-before for each changed or new test against 4529d7c
merged with main. Put the script and log under `evidence/` in the task dir and name them by absolute path. Then
`RESULT_STATUS` on its own line.

The constraints of the round 2 brief still apply: no live model, no request to 127.0.0.1:1234, and no special-casing of
fixtures.
