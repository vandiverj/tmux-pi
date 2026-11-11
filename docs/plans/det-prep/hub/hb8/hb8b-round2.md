# HB8b round 2: the raw stamp is taken before any normalization (Main, 2026-11-11)

The HB8b brief (`hb8b-brief.md`, this dir) still applies. Kind of round: rule-first (Opus); family verification-tree-identity
has now recurred past three rounds across HB8a/HB8b, so this round closes the class, not the case. Lane base for fail-before: 56506c7.

## Review F1 (review-coder-e18d734597-171440, realistic)
`tools/episodes/score.py:349–353`: `_stamp` is set *after* the five-second age filter has replaced the worker's own stamp
with the poll time. Result written 9.2, worker stamp 9, final-tree pass ending 9.25: polled at 9.4 gives stale 1 (right);
the same evidence polled at 15.1 gives `_stamp` 15.1 and stale 0 (wrong, credits the later pass). Main's restart and
readiness waits make delayed polls normal. Reviewer's probe: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-e18d734597-171440/probe.py. **Diagnosis:** HB8b's K3 rule said "keep the raw
stamp apart from the clamp" but did not name every normalization; the worker separated the clamp and missed the age filter
before it. Brief's fault for stating the case, not the class.

## The rule
1. Every time value a measure or check compares is derived from **source fields only** (the worker's `updated`, the poll
   `at`, a file mtime), never from another normalized value. Name each normalization in score.py (age filter, poll clamp,
   whole-second rounding, any other) and show in the report, as a table, which fields each reads and writes.
2. A normalization may only **widen** an undecided span; it may never turn a decided answer into a different decided one.
   When the worker's stamp is too far from the poll to trust, the change time is *undecided between the previous poll and
   this one* (or between the worker stamp and the poll), not "the poll time".
3. Both consumers the reviewer names (staleness and the worker false-claim check) read the same derived bounds.

## Attack (in the existing claim-time table, extended, not a new file)
Poll delays 0, 0.2, 4.9, 5.0, 5.1, 6, 30 s; worker stamp present/absent/after the poll/unparsable; previous poll before or after
the worker stamp; pass ending before, at and after the result; both consumers per row. Each row's expected answer is written
from the rule before running it. Run it on 56506c7 (fail-before: the delayed-poll rows must fail there) and on your tree.
Re-run the fake-episode comparison on **full objects** (review F2): compare the whole summary JSON of old vs new per run,
or narrow your report wording to what is compared; rerunning episodes is not needed if the stored summaries suffice.

## Verification (the worker tests once)
Run once, `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u
TP_STATE_DIR`) through `tools/agent/test-run.sh`, fresh short `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 600; exec @ARGV'`:
tests.test_episode_harness tests.test_session_env. Main runs the cells and discovery. Write report.md in the task dir with the
normalization table, the attack table (expected vs got on base and tree), files changed. No Claude or pi start; nothing to
127.0.0.1:1234 or any network; never the default tmux server. Edit only tools/episodes/score.py, tests/test_episode_harness.py
and evidence files in the task dir. Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth
files or config/local.json. Guard refusals: if a guard hook refuses a shell or inline-script edit, make the same edit with
Edit/Write and list it in the report; any other refusal means stop and report. Do not approve any permission or placeholder
prompt. Commit nothing. `RESULT_STATUS: complete` last.
