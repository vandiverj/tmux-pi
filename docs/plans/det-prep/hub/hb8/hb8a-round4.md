# HB8a round 4: dependencies come from reads, evidence carries its tree and time (Main, 2026-11-11)

Review review-coder-faac08e516-021100 (REQUEST_CHANGES), report, `probes.py`, `probes.log`: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-faac08e516-021100/`.
The HB8a brief and rounds 2-3 still apply. Bar: realistic use. Route rule-first; keep the change local to tools/episodes and its tests.

## Why the rule changes shape again
Round 3 checked records against a hand-written table (`MEASURE_RECORDS`/`check_records`), and its attack used the same table as
its oracle, so records the table forgot (test-run outcomes, the message journal, task metadata, worker transcripts used by claims,
Git readability) went unseen. This round derives each measure's dependencies from what it actually reads, and tests that with an
oracle that does not know the table.

## The rules
1. **One evidence reader; a measure depends on what it reads.** Every record the scorer uses (files under the run and state dirs,
   each transcript separately, `meta.json`, `messages.jsonl`, `test-runs/*`, events, requests, results, the clone and every Git
   call) goes through one reader that returns the value with a status: ok, missing, empty-but-the-log-says-it-ran, unreadable, or
   undated. A measure or check is `unknown` (with the reasons) when any read it made was not ok; this is recorded by the reader, not
   by a table. A record that is absent by design is ok only when its source shows nothing should exist (no `tp-agent message` call
   in Main's transcript → no journal is fine; a call with no journal → missing). Each listed transcript is checked on its own, not
   combined. A Git call with a nonzero exit is unreadable, never an empty diff. Keep the episode `records` list as documentation and
   a run-level `incomplete-records` check, but scoring no longer relies on it. Closes F1, F2, F10.
2. **An absence over a time window needs every candidate dated.** A negative check, a zero count, or "Main never acted" over a
   window is known only if every record that could match has a time; an undated candidate makes it unknown. A dated match still
   decides a positive. Applies to main_said, events, message count/after-state/to-done, recovery, waiting, pause claims and
   observations. Closes F9.
3. **A check result is keyed by (cwd, command, tree) and backs only claims after it on that tree.** The tree is known only from a
   record made at check time (a test-run record's tree, or a write history with times that fixes it); otherwise the result neither
   backs nor clears a claim (unknown). A pass on t2 does not clear a failure on t1 when the claim is made on t1. The final clean
   state is not proof of the tested tree. The runner's final check (after Main finished) feeds only end-of-run measures such as
   `completed`, never an earlier claim. Closes F3, F4, F5.
4. **Each execution and write has its own place.** Track `cd` per stage for executions and writes. An Edit/Write or redirect counts
   against the tree that contains its target path; a write outside the project (the task dir's report.md, a /tmp log) does not
   change a tested tree. Closes F6, F7.
5. **Only text that ran is an execution.** Strip shell comments before splitting stages. A stage after `&&` counts as run when the
   call's exit status is 0 or a structured record shows it ran; a stage after `||`, inside `if`/`case`, or after `&&` with a
   nonzero or unknown exit is uncertain: it never counts as a verification and makes a negative "did not run X" check unknown.
   Never execute shell to score it. Closes F8.
- **F11:** the dead-pane test waits for the pane's process to die directly (bounded), not for main_quiet under a scaled 5 s
  timeout. Keep the round-3 failure in the report as seen.
- **F12 (check):** narrow the report's universal claims to what the evidence shows; say the load explanation is Main's reading, not
  proven.

## Report
Fail-before against 4ba1430 for every probe in the review's `probes.py`, and a test per rule. The attack's oracle must not import
the scorer's dependency data. Use a differential oracle over the 14 fake runs (good and bad Main) plus the review's probe runs: for
each run, for every file under the run and state dirs (and each transcript), delete it, empty it, and strip its timestamps, one at a
time; rescore; and fail if any measure or check goes from one known value to a **different** known value (it must stay the same or
become unknown). Also break the clone's Git (`.git/HEAD`). Report the counts, every mismatch found and fixed, and the case list.
Keep the command identity cases (comments, `&&`/`||` with exit 0 and nonzero, cd per stage, writes outside the project, revert).
Rerun all 14 episodes fake and compare with round 3. The four cells as before, each log starting with `date -u`, `TMUX_TMPDIR`
(short /tmp path) and `git add -A && git write-tree`, then `git reset -q`. No `--live` runs; no Claude or pi start; private tmux
socket only; nothing to 127.0.0.1:1234 or any network. Search with safe_grep, read with Read/safe_read; never read
claude/mcp.json, .env* or auth files. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
