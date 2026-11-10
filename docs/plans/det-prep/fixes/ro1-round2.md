# RO1 round 2: the report is kept by what the reply says, not by guessing who asked (Main, 2026-11-09)

Review review-coder-c28747a7ad-220316 (REQUEST_CHANGES). Report, `reproduce.py` and `reproduce.log` are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-c28747a7ad-220316/`.
Run `reproduce.py` before you change anything. The visibility fields (laterReplies, laterReplyPath, record/index) and the tp-spawn
sentence were confirmed; keep them.

## The defects
- **F1:** a lagging transcript with earlier human entries overrides a known task-notification origin, so the notification's short
  reply replaces the full report (report.sh ~81-85).
- **F2:** state written before this change has no `resultTurn`; after turn 2 archives its short reply in `results/2.md`, the next
  submit archives the kept report into the same file and destroys the reply (report.sh ~58-59, 68, 98-111).
- **F3:** the engine's documented external origins (`source: user`, origin `composer`) are not recognised, so an external turn's
  reply is treated as unprompted (report.sh ~50, 62-67, 95).

## Main's decisions
1. **Drop origin guessing.** Whether a reply replaces result.md depends only on the reply: a reply that holds a valid
   `RESULT_STATUS:` line (the exact form tp-review and the watchers accept; define it once and share it) replaces result.md, after
   the old one is archived. A reply without one never replaces a result.md that holds one, whoever started the turn; it is archived
   and counted in laterReplies / laterReplyPath. A result.md with no RESULT_STATUS line is replaced as today. Remove the origin
   inputs (transcript scan, hook source/origin fields) from this decision. Packets already ask every turn to end with RESULT_STATUS,
   so a human-asked turn that follows the packet still updates the report. Say this in the tp-spawn sentence and the docs F5 names
   (`docs/legacy-workflow.md` ~137-145, `docs/plan.md` ~35, `bin/tp-spawn` ~540).
2. **An archive write never overwrites.** Each archived reply gets a file name that cannot already exist (create-exclusive; on a
   clash take the next number), so state from before this change, a missing `resultTurn`, or two submits close together cannot
   destroy an archived reply. Every reply the hook has seen is in result.md or under results/.
3. A RESULT_STATUS line that is malformed or incomplete (the open follow-up) counts as no valid line under rule 1.

## Attack the rule before you report
Input space, printed by code: prior state (no result.md; result.md with and without a valid RESULT_STATUS; state from before
RO1 without `resultTurn`; results/ already holding the next number) × reply (valid RESULT_STATUS of each status value, malformed
or bold or partial RESULT_STATUS, none, empty) × turn origin as the hook sees it (none, human, task-notification, `source: user`,
`composer`, unknown, lagging transcript) × sequence (one submit, two overlapping submits then both Stops, three turns). Expected
outcome per row: result.md content, every archive file's content, laterReplies. Run each through the real report.sh. Report
counts; keep worth-keeping rows in tests/test_claude_report.py; fold `reproduce.py` in.

## Report
- Fail-before for the new tests against db88881.
- `claude plugin test claude/plugins/tmux-pi`, and tests.test_claude_report tests.test_claude_plugin_module tests.test_lane_steps
  tests.test_sessionstart_compact tests.test_session_env tests.test_retry_packet tests.test_orchestration tests.test_tp_agent_inproc
  tests.test_main_notify tests.test_pane_send in all four cells: /usr/bin/python3 and /opt/homebrew/bin/python3, each outside
  (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`env TMUX=/tmp/x,1,1 TP_SESSION=dummy
  TP_TASK_ID=dummy`), after your last edit. Each log starts with `date -u` and the working-tree hash; time per file per cell.
- Lane whole-message (MS2) also edits report.sh (a separate `prompt_record` function for the prompt log). Keep your change to the
  result rules so the two merge cleanly.
- Never touch the default tmux server. Commit nothing; `git add` new files. `RESULT_STATUS: complete` as the last line.
