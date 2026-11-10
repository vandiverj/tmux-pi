# LS1c S1b round 2 (Main, 2026-11-09)

The review review-coder-52af2c36fb-141859 returned REQUEST_CHANGES. The work so far is checkpoint c55bea6 on
lane/structured-v2.

## F1 (defect, closes by code)
`outline` leaves out variadic parameters (`lib/summarize_structured.py:243`). For `def collect(first, *items,
**options):` it lists only `collect` and `first`.
- **The fix:** list every parameter except `self`/`cls`, in signature order: posonlyargs, args, vararg, kwonlyargs,
  kwarg. Each keeps the kind `parameter` and the same owner and line rule.
- **The test:** a regression test covering positional-only parameters, `*args`, keyword-only parameters and
  `**kwargs`, in `outline` and in the `user_message` names block.
- **Fail-before:** it fails on c55bea6. Keep the script and log under `evidence/` and name them by absolute path.

## F3 (note)
State in your report that `test_old_form_is_still_accepted` passes on the base by design: it is a regression guard,
not a fail-before test.

F2 (green after SE1 and AT1 are integrated) is Main's job.

## Done means
- Run tests.test_qual_summarize_structured and tests.test_qual_summarize_structured_v2 in the 4 cells:
  - with `/usr/bin/python3` and with `/opt/homebrew/bin/python3`;
  - outside tmux: `env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`;
  - inside tmux: `TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`;
  - each with `TMUX_TMPDIR=$(mktemp -d /tmp/s1b.XXXX)`.
- List each run's time.
- Report `RESULT_STATUS`.

Same rules as the S1b brief. No live model. Never read `.env*`.
