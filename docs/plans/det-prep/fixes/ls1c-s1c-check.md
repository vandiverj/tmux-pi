# LS1c S1c check: confirm the rebuilt lane closes the round-1 review (Main, 2026-11-09)

Lane code-facts was rebuilt on main f503858 as one commit b1f06fc. Its two files equal the round-2b tip 3245236 (backup branch
backup/code-facts-3245236). The rebuild was needed because a later-round review packet diffs against the previous round's
tree, which predates main's merges (~1.2M chars, over the 300k limit). This task gives the change a fresh first review against
main. You are a second pair of eyes on work already done, not its author. Fix only what is actually wrong.

Read first:
- the round-1 review, /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-f8f92bfdf7-150454/result.md,
  with `repro.py` and `repro.log` beside it;
- the briefs docs/plans/det-prep/fixes/ls1c-s1c-code-facts.md, ls1c-s1c-round2.md and ls1c-s1c-round2b.md;
- the round-2 and round-2b reports: `result.md` in tasks/coder-6a357383f2 and tasks/coder-c49f6a0650 (same tasks dir as
  above).

## Do
1. For each round-1 finding F1-F7 and F9, run the reviewer's reproduction (repro.py, or your own minimal one) against the
   lane, and give the before (312ca30) and after (lane HEAD) output.
2. Try to break each fix with at least one case the tests don't cover. The rule: a code line states only what the code
   shows, and an undecidable case emits nothing.
   - If you find a wrong line, fix it with a test that fails before the fix.
   - Otherwise report the cases you tried.
3. Check that nothing special-cases the calibration fixtures.

## Done means
- The six modules of the round-2 brief plus tests.test_qual_summarize_meaning pass with `/usr/bin/python3` and
  `/opt/homebrew/bin/python3`, both outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and
  inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
  - Rerun them after your last edit, if any, and list each file's time.
- Put the scripts and logs under `evidence/` in the task dir and name them by absolute path.
- `RESULT_STATUS` on its own line.

No live model and no request to 127.0.0.1:1234. Never read `.env*`, and never write that string in a shell command. Never
search `bin/` as a directory, or `config/`, `agent/` or `claude/` recursively.
