# C1: lane facts in the packet and LANE_BASE for lane workers

Lane `lane-facts`, step 1. Level: `mid` coder. Touches: `bin/tp-agent`, `bin/tp-spawn`, `lib/lane_commands.py`,
`tests/test_lane_create.py`, `tests/test_lane_lease.py`, `tests/test_retry_packet.py` (extend the module that already builds a
lane packet in a fixture; name it in the report).

## Why
438 of 665 tasks ran `git status` / `git log` / merge-base commands to orient; researchers spent 0.71M tokens on the lane base.
The lane record already holds `branch`, `worktree`, `base.ref`, `base.commit` (lib/lane_commands.py). `tools/agent/lane-info.sh`
and `pre-commit-gate.sh` read `LANE_BASE` from the environment, but nothing exports it (grep: readers only).

## Change
1. `lib/lane_commands.py`: `lane_facts(lane, step) -> str` rendering, from the record plus three git commands run in the
   worktree (`rev-parse HEAD`, `rev-list --left-right --count BASE...HEAD`, `diff --name-status BASE...HEAD`, plus
   `status --porcelain` for uncommitted paths):
   ```
   ## Lane (snapshot at 2026-11-07T18:02:11Z; refresh: tools/agent/lane-info.sh WORKTREE)
   lane: bs  step: 2  branch: lane/bs  worktree: /path/wt-bs
   base: lane/integrate 5f7b64a  (LANE_BASE is set to this sha)
   HEAD: 9c1e0d2 "Label revealed board cells"  ahead 3 / behind 0
   changed since base (3): M src/board.tsx, A src/cells.ts, M README.md
   uncommitted: none
   last closed step: 1 at 7a2b... (or: none)
   ```
   Cap the changed list at 20 lines and say `and N more`. A git failure renders one line `lane facts unavailable: <reason>`
   and never blocks the dispatch.
2. `bin/tp-agent` `launch()`: for a lane task pass the rendered text to tp-spawn (a new `--lane-facts-file PATH` or
   equivalent, not through the shell line) and add `--worker-env LANE_BASE=<base.commit>`.
3. `bin/tp-spawn`: render the section under `# Context` right after `Working directory:` and before the context files, so
   the facts come before any file content. Count it against `max_packet` like notes. `--worker-env LANE_BASE=...` must be
   accepted (check the key allow-list that refuses `TP_CTX_TOOLS`).
4. Tests: a fixture lane with two commits and one uncommitted file renders the exact block (stable fields; timestamp
   matched by regex); the `and N more` cap; the unavailable line on a broken worktree; `run.sh` contains
   `export LANE_BASE=<sha>`; a non-lane task gets neither the section nor the variable (byte-identical packet, extend the
   existing golden if one exists).

## Done means
1. A lane task's packet has the `## Lane` section with lane, step, branch, worktree, base ref and sha, HEAD sha and subject, ahead/behind, changed files (capped at 20), uncommitted paths, last closed step, and a snapshot timestamp with the refresh command.
2. The lane worker's `run.sh` exports `LANE_BASE=<base commit sha>`; `lane-info.sh WORKTREE` with no base argument then prints that base.
3. A non-lane task's packet and run.sh are unchanged.
4. A git failure while rendering gives the one-line unavailable form and the dispatch proceeds.
5. Tests for 1 to 4 exist in the named module(s) and fail without the change.
6. `docs/lanes.md` gains one paragraph describing the section and the variable (this step may edit that file for this purpose only).
