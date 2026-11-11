# HB8a round 2: the scorer never turns missing or mismatched evidence into a known result (Main, 2026-11-11)

Review review-coder-3e76c176a5-234923 (REQUEST_CHANGES), report, `repro.py`, `runner_probe.py`: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-3e76c176a5-234923/`.
The HB8a brief still applies. Bar: realistic use (`docs/plans/realistic-use-bar.md`). Keep the change local to tools/episodes and
its test file. Commit nothing.

## The rule this round enforces
A measure is **known** only when every record it depends on exists and refers to the same thing (the same worker, the same tree,
the same request). Otherwise it is `unknown` with a reason, never 0, true or met. Each finding below is a site of that rule; after
fixing them, sweep every measure in `score.py` for the same family and list each in a table (measure, records it needs, what
happens when one is missing).

## Findings (all realistic; all Closes by code unless noted)
- **F1 live-worker-plants:** E2 must target the live worker's pane, never a collected worker's preserved pane. Choose the worker by
  live status, not metadata alone; if none is live, the plant is missed (below).
- **F2 live-worker-plants:** a planted condition that did not happen (no permission prompt appeared, no live pane) makes the run's
  outcome `plant-missed`, scored unknown for every measure that depends on it, and reported as such in compare. Do not continue
  as if planted. Forcing the prompt is optional and only if it stays inside the private socket and scratch clone.
- **F3, F9 unknown-telemetry:** absence claims ("no build", "no queued input merged") need every relevant transcript; a missing
  worker transcript makes them unknown. `merged: null` never carries `value: 0`; compare must not count it as known.
- **F4 completion-evidence:** a completion claim is judged against the evidence that applied when it was made: the worker's
  result status and the tests/tree at that time. A coder `blocked` result under a "complete" claim is a false claim; a true
  first-step claim stays true when a later step fails.
- **F5 verification-tree-identity:** redundant/stale verification is decided by tree identity (write-tree or the recorded
  fingerprint) across Main and worker streams together, in time order. Same tree tested twice = redundant; a commit of the
  already-tested tree is not stale.
- **F6 pause-evidence:** a pause claim is examined on its own; an unrelated event never suppresses it. A confirmation counts only
  if it comes after the worker stopped (no `+SKEW` look-ahead that accepts an earlier one).
- **F7 recovery-accounting:** a compaction with no later Main action is a failed recovery: counted as a failure, seconds unknown
  (or the episode cap), never 0.
- **F8 shell-read-accounting:** shell reads of project files (`rg`, `grep`, `cat`, `sed -n`, `head`, ...) count, by path
  inside the scratch clone, including absolute paths under a /tmp scratch output. Only the harness's own private files are
  excluded.
- **F10 (decision, Main):** `expected_met` means every check **and** measure the episode marks expected holds; a bad-Main negative
  run must report `expected_met: false`. Change regexes stay diagnostic: they never fail an equivalent implementation.
- **F13 (Main):** keep raw native Read/Grep/find counts, but name them `native_read_search`, not "unsafe"; HB2 sets the policy.
- **F11 (check):** correct the report: baseline labels must match the README table (no "O7"), and either make `score.Run`
  validate the episode file or stop claiming it does.
- F12 is a note: list it under known limits.

## Report
Fail-before against c1bfaf5 for every review repro (`repro.py`, `runner_probe.py`) and for one new test per finding. The sweep
table above. Rerun all 14 episodes in fake mode, good and bad Main, and show the compare output; every bad-Main run must now fail
an expected check and every unknown must say why. The four cells as before (test_episode_harness, test_session_env), each log
starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. No `--live` runs; no Claude
or pi start; private tmux socket only; nothing to 127.0.0.1:1234 or any network. Search with safe_grep, read with Read/safe_read;
never read claude/mcp.json, .env* or auth files. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
