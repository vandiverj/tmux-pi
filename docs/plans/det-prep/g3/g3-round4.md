# G3 round 4 (Main, 2026-11-09)

Review review-coder-65193187aa-060509 is REQUEST_CHANGES with F1 to F3. Read its result.md and report.md in this task's
reviews. Every finding from rounds 1 and 2 is confirmed fixed; keep them fixed.

Main checkpointed round 3 as commit caf4385 on lane/g3-smoke.

## F1: the integration branch (a correction to the round 3 brief)

The round 3 brief said every branch name carries the run id. That cannot hold for the integration branch.
- The lane tools take it from the repository's lane config: `integration_branch` in `<repo>/.tmux-pi/lanes.json`, default
  `lane/integrate`.
- `lane remove` checks ancestry against that branch.
- A per-run name would mean editing the target repository's lane config. The smoke run must never do that.

The rule for the integration branch is therefore:
- **It is the repository's configured integration branch.** Read it from the real config, through
  `lanes.load_repo_config(repo)` or the CLI's output, not from a constant. Do the same for `branch_prefix` and
  `worktree_pattern` wherever the script builds or checks a lane branch name or a worktree path. If the script already reads
  them, show where.
- **Setup refuses if that branch already exists, before it makes anything.** The refusal message names the branch and tells
  the operator what to do: wait for the other run, or remove the branch themselves.
- **Cleanup deletes it only if this run made it.** That is already true; keep the test.

Tests:
- A disposable repository whose `.tmux-pi/lanes.json` sets a different `integration_branch` and a different `branch_prefix`:
  - the fake flow passes end to end using those names;
  - `lane/integrate` is never created;
  - the collision case refuses on the configured name.
- The existing `integration-exists` survival case stays.

README and report:
- say plainly that the integration branch is the one shared name, and why;
- explain how concurrent runs on one checkout behave: the second run refuses at setup and nothing is removed.

Do not change lib/ or bin/. If the flow cannot work with a configured name without a product change, report
`RESULT_STATUS: blocked` with the exact reason.

## F3: reporting gaps

- Fix the stale first-task wording at `g3_e2e.py:360-361`, which still says the operand names a file that does not exist.
  Describe the no-op trigger as it is now.
- In `SLOW_TESTS`, list every test over 5 s and every repeated check, with a timing each, not only files over 60 s. Time
  them one by one: run each slow test alone with `-k`, inside the four-cell runner's bounds.
- In the sweep table, follow each historical finding to where its code is now, not to whatever is at the old line number.

## Done means

Same as g3-e2e-smoke.md:
- four cells, each file under 60 s;
- the scripts you wrote and ran are listed;
- `RESULT_STATUS` is reported.

The isolation rules from g3-resume-1.md still hold:
- `TMUX_TMPDIR` is a temp directory set before the first tmux call;
- a private `-L` socket from the constructor;
- the PATH shim audit.

Never run the live mode.
