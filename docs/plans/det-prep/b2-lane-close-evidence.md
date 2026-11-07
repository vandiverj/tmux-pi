# B2: lane close checks the evidence records

Lane `evidence`, step 2. Level: `mid` coder. Touches: `lib/lane_steps.py`, `config/orchestration.json`,
`tests/test_lane_steps.py`, `docs/lanes.md` (the refusal table row and the config key). Requires B1.

## Why
`close_step` requires an approved review of exactly the current tree, but not that the gate ran on it or that any test ran.
A step can close with a stale or never-run gate.

## Change
1. Config: `lanes.require_evidence` in `config/orchestration.json` (default `false`, so existing projects are unchanged) and
   the same key honoured in the repository's `.tmux-pi/lanes.json`. When true, `evaluate` also requires, for a step whose
   tree differs from the last closed one:
   - a `gate.json` in the step's writer task dir (the newest task of the step) with status `fresh` for the current tree, exit 0,
     and no `failed` check;
   - at least one `tp-test-run:v1` record in that task dir that is `fresh` and has exit 0, unless the step's task was
     dispatched with `--tests none`.
2. Refusals follow the existing table style and name the command: `step S of lane NAME cannot be closed: gate record is stale
   (tree abc… at gate, def… now); run tools/agent/pre-commit-gate.sh WORKTREE` and `... no fresh test-run record; run
   tools/agent/test-run.sh WORKTREE -- CMD`. The owner's `--override-review` does not cover evidence; add `--override-evidence
   REASON` that records the reason in the close entry.
3. The close entry records `gate: {tree, exit}` and `test_runs: [{label, exit}]` (or `evidence: overridden`).

## Done means
1. With `require_evidence` false, `tp-agent lane close` behaves exactly as before (existing tests unchanged and passing).
2. With it true, a step with no gate record, a stale one, or one with a failed check refuses with the message naming the fix.
3. With it true, a step with a fresh passing gate but no fresh test-run refuses unless the task had `--tests none`.
4. `--override-evidence REASON` closes and records the reason; the plain close entry records gate and test-run summaries.
5. Tests in `tests/test_lane_steps.py` cover 1 to 4 and fail without the change.
6. `docs/lanes.md` has the config key and the new refusal rows.
