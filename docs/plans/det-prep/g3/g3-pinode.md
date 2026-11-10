# G3 round 7: the live session carries Main's pi interpreter setting (Main, 2026-11-09)

## What happened
Live runs 6 and 7 (b6b43c4, battle-soccer) passed setup, dispatch, prompt, decline (settled `declined` by UserPromptSubmit, the
redirect confirmed submitted) and retry, then failed at `review`:
`tp-review coder-… exited 1: no such pane: %111` / `reviewer 'pi' … could not start`.

Cause, reproduced by Main: a pane made by `tmux new-window` takes its environment from the tmux server and session, not from the
process that ran tmux. `bin/tp-pi` runs pi on `${TP_PI_NODE-$HOME/.nvm/versions/node/v24.16.0/bin/node}`. Main's session has
`TP_PI_NODE=` (empty: run `pi` from PATH) in its tmux session environment; on this machine the pinned nvm Node is not installed.
The smoke's own session (`new-session -d -s <run> …`, tools/smoke/g3_e2e.py ~line 924) does not get that variable, so tp-pi exits
at once with "pi is pinned to …". The pane then vanishes before `tp-spawn` sets `remain-on-exit`, which is why the error read
"no such pane" (that race is a separate follow-up in bin/tp-spawn; do not change bin/tp-spawn).

## Main's decision
- The live smoke runs workers "inside the tmux server that Main uses", so its session must carry Main's settings for how a
  worker starts. When the caller's environment has `TP_PI_NODE` (set, even empty), the session the smoke creates gets the same
  value in its session environment, in the same tmux call that creates it (`new-session -e NAME=VALUE`, tmux ≥ 3.2; refuse
  clearly on an older tmux rather than silently skipping). When it is unset, nothing is added and tp-pi's default applies.
- Name the variables carried in one tuple (today only `TP_PI_NODE`), documented in docs/plans/det-prep/g3/README.md with why.
  Do not carry the whole environment.
- Setup checks it: after creating the session, `show-environment -t =<run> TP_PI_NODE` equals the caller's value (a check row,
  named like the others).
- Add the live setup line to the README: when pi reviewers need a Node other than the pinned one, Main's session sets
  `TP_PI_NODE` and the smoke carries it.

## Attack the rule before you report
Input space: caller `TP_PI_NODE` unset / empty / a path / a path with a space / a value with `=` and quotes × fake and live
command construction (the argv tmux receives) × the check row (match, mismatch tamper). Print by code, run each, expected
outcome and reason, counts. Keep worth-keeping rows as tests in tests/test_g3_e2e.py (fake mode: the fakebin tmux records argv).

## Report
- Fail-before against b6b43c4 for the new tests.
- tests.test_g3_e2e tests.test_g3_e2e_tamper tests.test_session_env in all four cells (both interpreters, outside and inside
  tmux) after the last edit; time per file per cell; SLOW_TESTS.
- Never run the live smoke from your pane; Main runs it. Never touch the default tmux server: tests use TMUX_TMPDIR from
  `mktemp -d` and a private `-L` socket.
- Commit nothing; `git add` new files. `RESULT_STATUS` as the last line.
