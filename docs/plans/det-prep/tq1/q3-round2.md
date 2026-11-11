# Q3 round 2: only input-only members are folded (Main, 2026-11-11)

The Q3 brief (`q3-folds.md`, this dir) still applies. Kind of round: mechanical (Sonnet). Lane base for fail-before: 924b393.

## Review F1 (review-coder-198658e0d3-173717)
Folds that combine differing setups or assertions: `tests/test_lane_create.py:164–202,477–489` (only the existing-path row
checks keep.txt through an optional callback; CLI rows use different predicates), `tests/test_lane_other_paths.py:45–84`,
`tests/test_lane_lease.py:111–140`, `tests/test_review_rounds.py:179–197,542–557,1225–1253` (conditional assertions,
heterogeneous setups). **Diagnosis:** worker misread; the brief already said a member qualifies only with the same setup and
the same assertions. A row needing an optional callback, an `if` on the row, or a per-row predicate is the sign.

## What to do
1. Audit **every** removed method (not only the reviewer's lines): a fold stays only if each row runs the same setup code and
   the same assertion statements, with only data values differing (no per-row callbacks, no `if` choosing assertions, no
   per-row functions). Otherwise restore the original methods byte-for-byte from 6edf2d3 and list them "kept, differs: <why>".
   Keeping a legitimate partial fold (the qualifying members only) is fine.
2. Report (review F2): a table of every removed method -> row, with the old assertion line numbers (in 6edf2d3) and the new
   ones, the bug commit where `git log -L` shows one, and per-module totals (methods, rows) before/after. Save each mutation
   as a patch file under evidence/ (the diff you applied), with its failing output.
3. Main measures covered lines on the preview (not you).

## Verification (the worker tests once)
As in q3-folds.md: the changed modules plus tests.test_session_env once, through tools/agent/test-run.sh, outside tmux, fresh
short TMUX_TMPDIR, `perl -e 'alarm 900; exec @ARGV'`. Edit only the four test files and evidence files in the task dir.
Guard refusals: a refused shell or inline-script edit may be redone with Edit/Write and listed; **any other refusal (including
a refused command you could rephrase) means stop and report**. Approve no prompt. Commit nothing. No background shells.
`RESULT_STATUS: complete` last.
