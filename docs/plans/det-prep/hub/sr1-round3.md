# SR1 round 3: one validation, every review, every boundary (Main, 2026-11-11)

Round 2's review (review-coder-66d2e23273-152233, `result.md`, `report.md` and its probes in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-66d2e23273-152233/`)
is REQUEST_CHANGES: F1, F2 (incomplete-stop-findings, recurring), F3 (familyless-stop-authorization), F4/F5 checks.
The round-1 and round-2 briefs (sr1-stop-rule.md, sr1-round2.md) still apply except where a rule below replaces them.
**Main's diagnosis: worker misses of stated rules.** Round 2 rule 2 ("within its section"), rule 4 ("the same extraction
everywhere": stop_plan and every check) and rule 2's "a code finding with no family refuses" were each applied in some places,
not all. **Route rule-first:** the rules below are invariants of one function, not more cases.

## The rules
1. **One function decides a review.** `stop_review(text) -> (findings, refusal)` (name yours): the only place that turns a review's
   text into its verdict, its findings (reviewer id, family, closes-by) and a refusal reason. Authorize (`stop_plan`), every
   check in `stop_rule_checks`, and the selection of which reviews count all call it; nothing on the stop-rule path reads
   `review_verdict`/`family_sweep`/the legacy label reader to decide a review's findings or to skip a review. Say in the report
   every call site you changed and grep to show no other remains.
2. **Which reviews count (review F2).** Every review of the step's closed tree (and, for authorize, every review the
   authorization binds to) goes through rule 1 before anything is excluded. A review is left out only if rule 1 proves it
   approves; a check-only label from the old reader excludes nothing.
3. **Section boundaries (review F1).** A finding's section starts at its counted heading and ends at the next line that is any
   Markdown heading (`#`..`######`), any counted finding heading or bold lead, or end of text. `Family:` and `Closes by:` lines
   belong only to the section they are in; a line outside every finding section belongs to none. A finding missing either line in
   its own section refuses (rule 2 of round 2). Two `Closes by:` (or two `Family:`) lines in one section refuse.
4. **Validation is shared (review F3).** The refusals (zero findings in a rejecting review, unclosed fence, duplicate id, missing
   or doubled `Closes by:`, code finding without a family) are made in rule 1's function, so authorize refuses exactly where close
   does. Authorize never calls confirmation for a review that close would refuse.
5. **Report correction (review F5).** Correct `report.md` on the old-code cases (attack.log shows `[F1,F2]` and `[F1,F3]`, not `[]`).

## Tests (tests earn their place)
Extend `StopRuleTests`' table, same fixture, every row run through authorize and close: the review's F1 (last code finding
without closure followed by `## Owed before close` with a check item), its F2 (a second rejecting review: check finding,
Verification, bold `**F9 ...**` code finding in a new family), its F3 (code finding without family beside an authorized one),
doubled `Closes by:`. Fail-before on 9c1e619 for these rows. No new file.

## Verification (the worker tests once)
As round 2: tests.test_lane_steps, tests.test_lane_records, tests.test_family_sweep and tests.test_session_env once through
`tools/agent/test-run.sh`, `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u
TP_TASK_DIR -u TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp, log starting with `date -u`, `TMUX_TMPDIR` and `git add -A &&
git write-tree`, then `git reset -q`; find the log by glob. Main runs the cells and the discovery. No Claude or pi start; nothing to
127.0.0.1:1234 or any network; private tmux `-L` sockets only. Never run the commands against this repository's real lanes. Do not
edit config/, interfaces.md or release-gates.md. Edit only lib/lane_steps.py, lib/family_sweep.py (shared helpers only, no
behaviour change for its other callers), docs/lanes.md, tests/test_lane_steps.py; bin/tp-agent with the Edit tool only if
needed. Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. If a
guard hook refuses a command, stop and report it; do not work around it. Do not approve any permission or placeholder prompt; stop
and report it. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
