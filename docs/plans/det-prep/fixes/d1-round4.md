# D1 round 4 (route: design) — the reviewer sees staged content, not just its path

Round 3 left one finding (family staged-diff-clarity). When a file has content staged and then restored in the worktree, the round-1 diff omits the staged content and only names the path (`lib/review_rounds.py:377-385`). `render_scope` (`lib/review_rounds.py:579-626`) also never says the diff covers the worktree only, while the diff header says staged files are included.

## Rule
The reviewer packet must show every change the author left in the index or the worktree, as content, and the scope must state exactly what each diff section covers.

## Change
1. Round-1 diff, `diff_now`:
   - Keep the worktree-vs-HEAD diff as it is.
   - For every path where the index differs from both HEAD and the worktree, append a section headed `Staged, not in the working files:` with that path's `git diff --cached -- PATH` content. It is redacted and capped like the main diff.
   - Remove the path-only warning.
2. `render_scope`: state in one line what the main diff compares (worktree vs base), and that any staged-only section is staged-vs-HEAD. Make the diff header agree with that line.
3. Later rounds: apply the same rule wherever the round-N diff is built (`lib/review_rounds.py`), or say in the scope that a later-round diff is worktree only. Prefer the first.

## Tests (tests/test_review_rounds.py, plus tests/test_tp_review.py only if the packet text changes)
A table of index/worktree states. For each state, assert the content that must appear and the scope line:
- staged then restored (the reviewer's case);
- staged and further edited;
- staged only;
- unstaged only;
- a new file, staged then deleted from the worktree;
- a binary file staged.

Show the new table fails on the round-3 code (use a copy of the old file) and quote the failure count.

## Report
- For each module: the command, the test count, the seconds and the log path (output redirected to `$TP_TASK_DIR/tests.log`).
- A sweep table of every site that builds or labels a reviewer diff.
- Do not commit.
