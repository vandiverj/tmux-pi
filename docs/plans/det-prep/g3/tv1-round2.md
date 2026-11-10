# TV1 round 2 (Main, 2026-11-09)

Review review-coder-6e159aa869-084845 is REQUEST_CHANGES. Read its result.md and report.md in this task's reviews. Main
checkpointed round 1 as commit 8b1a35b.

For each finding: fix it, then sweep for every other site of the same family. Give one sweep table per family, covering
every site: fixed, already right, or not an instance, each with evidence.

## F1 (bench-trust-integration): the bench must keep its behaviour
- In `lib/bench/runner.py`, the wait treats the new `needs_input` from inspect as terminal before it checks the trust
  dialog. Restore the old result: `error: trust-dialog` with the remediation, and a cleanup that stops the unfinished
  worker instead of collecting it.
- Prefer the inspect field `trust_dialog` as the signal where the bench reads inspect output, and keep the bench's own
  capture check.
- **Sweep.** Every consumer of `inspect` / `list` status in the repo that treats `needs_input` as terminal or as "asked a
  question": the bench, tp-review, the trial status and cleanup, tp-list, the status pane, the G3 smoke, and any other
  the grep finds. For each, say what it does with a trust-dialog `needs_input` and whether that is right.
- **Test.** The bench fixture's fake inspect returns the new response (`needs_input` with `trust_dialog`). The result is
  `error: trust-dialog` and the worker is stopped, not collected. It must fail on 8b1a35b.

## F2 (scan-starvation): one timeout must not hide later dialogs
- Remove the scan-wide stop in `lib/trust_dialog.py`. Keep total latency bounded differently: for example a per-row
  timeout plus an overall budget for the whole list. When the budget is spent, mark the rows that were not checked as
  "not checked" in the JSON, rather than silently leaving them as they were. State the bound in the docstring.
- **Test.** A first pane whose capture hangs and a second pane showing the dialog: list and status report the second
  as `needs_input`. Also show the overall bound holds with N hanging panes. It must fail on 8b1a35b.

## F3 (bootstrap-command-quoting): every printed command is quoted
- `tools/smoke/g3_e2e.py` prints the bootstrap directory unquoted. Use `shlex.quote` on every operand, as
  `trust_dialog.message` already does.
- **Sweep.** Every place TV1 prints a command for the owner to run (inspect detail, tp_detail, list, G3, bench, README).
- **Test.** A directory with a space and a `$`: the printed command, run through `shlex.split`, gives exactly the
  directory as one argument. It must fail on 8b1a35b.

## F5: evidence provenance
In the report, name the log or script for:
- the matrix runner and its environment;
- the three-failure mutation;
- the pre-change G3 run.

If one no longer exists, rerun it and keep the output in the task dir.

F4 (the index and notifier do not show `needs_input`) is a follow-up Main tracks. Do not change it in this round.

## Done means
- Four cells green over the same 13 files as round 1, plus any you add, with each file's time.
- Fail-before against 8b1a35b for each new test.
- The scripts you wrote and ran are listed.
- `RESULT_STATUS` is reported.

The rules from trust-visible.md still hold:
- never answer the dialog;
- never touch the Claude profile;
- no live mode, model or bench run;
- `TMUX_TMPDIR` set first and a private `-L` socket for any test that calls tmux;
- never read `.env*`.
