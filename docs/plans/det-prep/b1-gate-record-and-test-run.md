# B1: gate record with a tree fingerprint, and a real test-run

Lane `evidence`, step 1. Level: `mid` coder. Touches: `tools/agent/pre-commit-gate.sh`, `tools/agent/_common.sh`,
`tools/agent/test-run.sh` (replaces the A1 stub), `lib/gate_record.py` (new: read, validate and compare records),
`agent/roles/coder.md`, `agent/roles/tester.md`, `tests/test_agent_tools.py`, `tests/test_gate_record.py` (new),
`docs/agent-tools.md` (the two script rows and a "Records" paragraph). Requires A1 merged.

## Why
Nothing records which checks ran on which tree. The gate exits 0 when a tool is missing, when nothing ran, and when the tree
changed after it ran; gitleaks scans commits only. coder.md tells workers to cite a `tp-agent test-run` record that does not
exist. Workers poll with `sleep` and `until grep` because nothing waits for them.

## Change
1. Tree fingerprint helper in `_common.sh`: `tp_tree_fingerprint WORKTREE` = `git write-tree` on a temporary index that
   includes tracked changes and untracked non-ignored files (`GIT_INDEX_FILE=tmp; git add -A; git write-tree`), plus HEAD sha.
   Deterministic for the same content.
2. `pre-commit-gate.sh` writes `gate.json` next to the log and, when `TP_TASK_DIR` is set, to `$TP_TASK_DIR/gate.json`
   (overwriting; the previous one is kept as `gate-<ts>.json`):
   `{"schema":"tp-gate:v1","worktree","base":{"ref","sha"},"head","tree","files":[...],"checks":[{"name","status":"passed|failed|missing|not_applicable","exit","seconds"}],"exit","started","finished","log"}`.
   `missing` = tool not installed, `not_applicable` = no config / no matching files. New flag `--require NAME...`
   (default none): a required check that is `missing` fails the gate (exit 1) and says so. When zero checks ran, exit 0 but
   the summary says `no check applied` and the record shows it. gitleaks: when installed, also `gitleaks dir` (or the
   equivalent no-git mode) over the changed dirty and untracked files, recorded as a separate check `gitleaks-dirty`.
3. `tools/agent/test-run.sh WORKTREE [--timeout S] [--label NAME] -- CMD ARGS...`: runs the command in the worktree
   synchronously with a hard timeout (default 900 s; on timeout exit 124 and say so), tees output to a log, prints the last
   20 lines and the `exit=N log=PATH` line, and writes `$TP_TASK_DIR/test-runs/<n>.json` (n increments; when `TP_TASK_DIR` is
   unset, beside the log) with `{"schema":"tp-test-run:v1","label","cmd":[...],"cwd","head","tree","exit","timed_out","seconds","log","summary":"<last matching line of a known runner: 'Ran N tests', 'Tests:', 'Test Files'>"}`.
   No backgrounding, no polling needed.
4. `lib/gate_record.py`: `load(path)`, `validate(record)` (schema, required keys), `current(worktree) -> tree` using the same
   fingerprint rule (call the shell helper or reimplement identically and test equality against it),
   `status(record, worktree) -> fresh|stale|invalid` and `missing_required(record, names)`. B2 uses it; `tp-agent inspect` may
   show it later.
5. Role prompts: coder.md's "tp-agent test-run record" becomes `tools/agent/test-run.sh` and its record path; tester.md says
   the same. Nothing else in the prompts changes.
6. Tests: fixture repo in `tests/test_agent_tools.py` (reuse the existing gate fixture): record written with the right
   statuses for a missing tool, a not-applicable check, a failing check; `--require` fails on missing; fingerprint changes
   when an untracked file is added and is equal for equal content; test-run writes the record, honours the timeout (use a
   `sleep 5` command with `--timeout 1`; mark `timer-ok:` since the timeout is the behaviour), and captures the summary line
   for a vitest-style and a unittest-style output sample.

## Done means
1. `pre-commit-gate.sh` writes a valid `tp-gate:v1` record with per-check status, head, tree fingerprint and base, beside the log and in `$TP_TASK_DIR`.
2. `--require gitleaks` fails the gate with exit 1 when gitleaks is missing; without the flag it is `missing` and the gate's exit is unchanged.
3. Dirty and untracked changed files are scanned by a `gitleaks-dirty` check when gitleaks is installed (skipped as `missing` otherwise).
4. `test-run.sh` runs a command to completion or timeout, writes a `tp-test-run:v1` record with exit, seconds, tree and summary, and never backgrounds.
5. `lib/gate_record.py` reports `fresh` for an unchanged tree and `stale` after any tracked or untracked content change.
6. coder.md and tester.md name the real script; no reference to `tp-agent test-run` remains in `agent/`.
7. Tests for 1 to 5 fail without the change; `docs/agent-tools.md` documents the records, the flag and the new script.
