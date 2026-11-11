# SG1 round 13 (step SG1b): reconcile the G3 and placeholder tests with worker refusal (Main, 2026-11-11)

SG1 round 12 was approved (review-coder-f254667eac-011332, APPROVED_WITH_NOTES) and merged into local main (8575327, not pushed).
A full single-process run of `/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py'` on a clean worktree of 8575327
then failed 90 tests that pass on fa40b30 (main before the merge): 89 in `tests/test_g3_e2e.py` (`HookTriggerTest`) and 1 in
`tests/test_placeholder_use.py` (`test_placeholders_do_not_override_the_credential_gates`). Each expects `ask` for a worker's
sensitive shell read and now gets `deny`. SG1's own cells never ran these modules. Main pushes nothing until this is fixed.
Bar: realistic use. Lane safe-search; base for this round is main 8575327 merged into the lane (bring the lane up to main first).

## What is right
SG1's decision is the intended one: in a worker, a shell read of a sensitive name is refused with a safe-tool pointer; Main is still
asked. Do not change any decision in `lib/sensitive_read.py`, `bin/tp-sensitive-read`, `lib/redact_hook.py` or
`agent/extensions/sensitive.ts` to make these tests pass. If a decision looks wrong while you work, stop and report it with the
command, context and both decisions.

## The work
1. **G3 hook tests (`tests/test_g3_e2e.py`).** Where the worker's G3 trigger (`g3.PROMPT_COMMAND`) now gets refused, assert the
   refusal as it is: deny in every mode, the reason names the safe tool, the file is untouched, and no pending
   `permission-request.json` is left that would make the task look like it needs input (check what the code actually writes and
   assert that). Keep real coverage of the ask → decline/turn → settle machinery: drive it with an event that still asks a worker
   under SG1 in normal use (find one from the current code and the round-12 cause table, e.g. a cause-bearing project script or
   Git call), or with Main's context where it still asks. Every ask/settle property the 89 tests checked must still be checked
   somewhere; list each old test → its new form in the report.
2. **Live smoke (`tools/smoke/g3_e2e.py`).** Release gate G3 (owner's `docs/plans/det-prep/release-gates.md`, do not edit it)
   needs "a permission prompt that is declined and redirected". Make the smoke's first task use a trigger that still raises a
   worker permission prompt under SG1, with the same no-secret guarantees (absent file, sentinel checks). If no realistic worker
   trigger still prompts, say so and propose the gate wording change for the owner instead of forcing one. Do not run the smoke
   live; its unit tests and `--dry`-style paths only.
3. **Placeholder test (`tests/test_placeholder_use.py:325-331`).** In a worker, the network call that sends a sensitive file is
   refused; assert deny and the reason. Keep the `ask` + `network` + `tp-secret-exec` assertions for the context that still asks
   (Main), so the credential-gate property stays covered.
4. **Round-12 note F1 (check):** correct the report mappings for sweep rows 7, 10, 11, 12 as the review states.
5. **Whole-repo check.** Search the other test modules for tests that call the hooks or classifier in a worker context and
   expect `ask` (safe_grep for `"ask"` near `TP_TASK_ID`/`permissionDecision`); list each and whether it passes.

## Report
Fail-before: the 90 failures on 8575327 and pass on fa40b30 (cite the module runs). Then: the full single-process discovery on a
clean tree (`git add -A`, write-tree, run, `git reset -q`) with 0 failures, plus the four cells (py3.9/3.14 × inside/outside tmux)
over the SG1 modules and `tests.test_g3_e2e tests.test_placeholder_use tests.test_session_env tests.test_orchestration`, each log
starting with `date -u`, `TMUX_TMPDIR` (short /tmp path) and the write-tree. Private tmux socket only; never the default server.
No live Claude/pi/smoke runs; nothing to 127.0.0.1:1234 or any network. Search with safe_grep, read with Read/safe_read; never
read claude/mcp.json, .env* or auth files. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
