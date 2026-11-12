# PF1c: one git call for the review diff, and timers reuse prepare's scope (Main, 2026-11-12)

PF1a (coder-9155b7e965; `report.md` and `evidence/` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/coder-9155b7e965/`)
measured a 200-file `tp-review`: `review_rounds.py prepare` 3.2 s, of which 247 `git diff` children (about 8.8 ms each) are the
bulk, and `timers` 0.33 s repeating scope work. PF1b (merged 7bb49ad) did proposals 1, 4 and 5. This lane does proposals 2 and 3.
Owner priority: test and review speed ("understanding where work waits and avoiding repeated verification"). Lane perf-review,
base main. Kind: design (the batch must keep a safety property). Bar: realistic use (`docs/plans/realistic-use-bar.md`).

Read first: `lib/review_rounds.py` blob_patch (:310), blob_text (:328), entry_patch (:355), safe_diff (:678 and its docstring),
prepare (:1052), scope_lines (:1190), scope_text (:1236), added_lines (:1351), timer_waits (:1378), main (:1415);
`bin/tp-review` lines 360-460 (prepare, timers and scope calls); `tests/test_tp_review.py` (the 200-file and rounds cases).

## Rules
1. **The packet text does not change.** For every input, the text `safe_diff` returns and the `timers` file are byte-identical to
   what main's code produces. The oracle is main's code run on the same repository state, never a hand-written expectation.
2. **No path reaches git for content.** Today each patch is made from two blob ids, "never from a path given to git, so a path that
   turned into a directory (or the reverse) cannot pull in entries that were not checked." The batched form keeps that: git is
   given only blob ids (or objects this code builds from the checked entries alone, for example a tree made by `git mktree` from
   the safe entries under names this code chooses), never a pathspec, and never a real path whose attributes could change the
   output. State how the chosen form keeps this, and what happens with a `.gitattributes` in the repository (`* binary`,
   `* -diff`, `*.txt diff=x` with a textconv/external driver configured): the output must equal today's for each.
3. **Splitting is exact.** The batched output is split back per entry by structure this code controls (names it chose, or a
   count), never by searching file content: a file whose content contains `diff --git `, `@@`, `Binary files`, or a NUL must not
   shift a boundary. Binary, empty, added, deleted, mode-only, type-change, submodule, symlink and whole-file (rewrite) entries
   all keep today's section. Batch `blob_text` the same way if it is called per file (one `git cat-file --batch` call), or say why not.
4. **Failure stays a refusal.** If the batched call fails, raise `Refused` as today (same message shape); no silent fall-back to
   a partial diff. A fall-back to the per-file form is allowed only if it produces the identical text, and the report says when.
5. **Timers reuse, not recompute.** `timers` gets the scope `prepare` already computed (bin/tp-review passes `scope-input.json`
   the way `scope` already takes `--input`) only where that plan is the one `timer_waits` would compute today. Check this first:
   in a later round prepare's plan is the round's diff (`make_plan(top, last tree, "", now tree, ...)`), while `timer_waits`
   calls `scope_lines(workdir, lane)` with no plan, which is the round-1 plan. Where they differ, keep today's behaviour (rule 1)
   and say so; do not change what the timers file covers in this lane. Report the saving per round kind.
6. **Process count is the measure.** Report git children and wall time for the 200-file case and a 3-file case, before and
   after, with the same harness PF1a used (`evidence/scripts/harness_review.py` in its task dir).

## Input-space attack table (required in report.md, built by code, one row per cell, each row -> the test row that proves it)
Entries: added, deleted, modified (small edit), modified (rewrite -> whole-file section), mode-only, type change (file <-> symlink),
binary, empty file added, empty <-> non-empty, file without trailing newline, CRLF, non-UTF-8 bytes, content holding `diff --git`,
`@@ -1 +1 @@` and `Binary files` lines, submodule, a path that became a directory and the reverse, unicode/space/quote/backslash
path, sensitive (withheld) path, generated path, frozen copy. Sizes: 0, 1, 3, 200, 1000 entries. Repos: no `.gitattributes`, each
attribute form in rule 2, `diff.external` and `core.attributesFile` set in the repo config. Rounds: round 1, later round with a
reviewed state, later round without one. Expected per row: identical text to main's code (rule 1), and the git child count.

## Tests (tests earn their place)
Extend the existing differential/rounds cases in tests/test_tp_review.py (and test_review_rounds if one exists) with a
table-driven method whose rows compare the new code's output to main's per-file form computed in the test (keep the per-file
function reachable for that, or embed the old form in the test); the full attack table stays a report artifact. Fail-before is
not expected (behaviour-preserving); show process counts before/after instead.

## Verification (the worker tests once)
`tests.test_tp_review`, the review_rounds tests, and `tests.test_session_env` once, `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), fresh short `TMUX_TMPDIR` under /tmp,
`perl -e 'alarm 900; exec @ARGV'`, through `tools/agent/test-run.sh`, log starting with `date -u`, `TMUX_TMPDIR` and
`git add -A && git write-tree`, then `git reset -q`. Foreground, one after another. Main runs the cells and discovery. No Claude
or pi start; nothing to 127.0.0.1:1234 or any network; never the default tmux server. Do not edit config/. Search with safe_grep,
read with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. **Guard refusals:** a read-only
analysis script goes in your evidence/ dir as a `.py`, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`; any other
refusal: stop and report. Approve no prompt. Commit nothing. No git notes. No background shells. `RESULT_STATUS: complete` as the
last line.
