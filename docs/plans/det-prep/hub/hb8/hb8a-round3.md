# HB8a round 3: fail closed per run, one identity per task and per command (Main, 2026-11-11)

Review review-coder-227e1af42a-005423 (REQUEST_CHANGES), report, `repro.py`, `more_probes.py`: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-227e1af42a-005423/`.
The HB8a brief and round 2 still apply. Bar: realistic use. Route rule-first; keep the change local to tools/episodes and its tests.

## Why the rule changes shape
Round 2 fixed each missing-record site and the review found seven more. Proving each measure's records one by one does not
converge (the same lesson as SG1 rounds 8-10). This round fails closed at the run level and gives each thing one identity.

## The rules
1. **Record completeness is checked once, per run, before scoring.** Each episode file lists the records its scoring needs (Main
   transcript, every dispatched worker's transcript and result, the state directory, the episode log, the request/event streams
   when the episode uses them, the planted condition). `score.py` first builds a manifest of what exists. Any measure whose
   required records are not all present and readable is `unknown` with the missing list as the reason; a run missing a record
   that every measure needs is `incomplete-records` and all its measures are unknown. An existing empty transcript is not
   evidence of absence unless the run log shows that agent ran. A missed plant makes every measure that depends on it unknown
   with the reason `plant-missed` (never n/a). Closes F1, F2, F9, and F11 with it.
2. **One identity per task.** Tasks are keyed by task id, never `(role, lane)`. A task's result counts against a claim unless
   the record says another task superseded it. Closes F3.
3. **A claim is judged at its own time.** Use every check record (build, tests, ...) up to the claim time, not only the latest; any
   failed required check before the claim with no later passing run on the same tree makes a "passed/complete" claim false. Later
   work never changes an earlier claim's verdict; a record with no time cannot back a claim (unknown). Closes F4, F5.
4. **One identity per command execution.** Verification and build/test counts come from structured test-run records (`cmd`, cwd,
   tree) when they exist, and otherwise only from Bash tool calls whose executed command (first word of a pipeline stage, after
   `cd`/env assignments) is the runner; text inside arguments (`grep 'npm run build' README.md`) is never an execution. A record's
   label never hides its `cmd`. Distinct commands (different targets/args) are distinct. A tool call that both writes and checks
   (`sed -i … && npm test`) records the write first. Tree identity is the recorded tree (or write-tree), compared in time order
   across all streams; test → edit → revert → claim on the tested tree is verified and not stale. Closes F6, F7.
5. **Shell-read counts are diagnostic only.** They never feed `expected_met` or a pass/fail measure. Count a read only for a file
   path operand (or `-f`/`--file` operand) resolved against the tracked cwd; stdin-only stages (`npm test | rg FAIL`) are not
   reads. Mark unhandled shapes as uncounted in a known-limits line. Closes F8 at its proper weight.
- **F10 (check):** correct the cell timing (74 tests in 16.212 s for system-Python inside) and the report's counts; Main updates
  its own evidence.

## Report
Fail-before against 48cd1d6 for every review repro (`repro.py`, `more_probes.py`) and a test per rule. A table: each measure,
the records it requires, the identity it uses. An attack over record presence: for every episode, delete each required record in
turn (and make it empty), and show every dependent measure becomes unknown with a reason and none becomes known; plus the command
identity cases (mention vs execution, labels, distinct targets, write+check in one call, revert). Rerun all 14 episodes fake, good
and bad Main, and compare. The four cells as before, each log starting with `date -u`, `TMUX_TMPDIR` (short /tmp path) and
`git add -A && git write-tree`, then `git reset -q`. No `--live` runs; no Claude or pi start; private tmux socket only; nothing to
127.0.0.1:1234 or any network. Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env* or auth files.
Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
