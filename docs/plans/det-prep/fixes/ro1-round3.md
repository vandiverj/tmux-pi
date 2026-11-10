# RO1 round 3: a Stop with no open prompt keeps a reply once, however late it arrives (Main, 2026-11-10)

Review review-coder-a6d0364bbf-235539 (REQUEST_CHANGES). Report, `reproduce.py` and `reproduce.log`:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-a6d0364bbf-235539/`.
Run `reproduce.py` first. Everything else in round 2 was confirmed (round 1 F1-F5 closed, preservation, fallbacks, visibility
fields, docs, and Main's preview resolution keeping both `record` and `ext`); do not rework it. F2 (overlapping valid second Stop is
archived) stays as it is.

## The defect
- **F1:** report.sh:105-110. A Stop with no open prompt is skipped only when its reply equals `result.md` or the latest
  `laterReplyPath`. After a statused report, then later replies A and B, a delayed duplicate Stop for A archives A again (`4.md`),
  `laterReplies` goes 2 -> 3 and `laterReplyPath` moves from B back to A.
- **F3:** the attack oracle (evidence/attack.py:134-180) checks that each reply is somewhere and that old archives survive, but not
  the complete expected set of archives, so an extra duplicate archive passes.

## Main's decision
**A reply is identified by its bytes. A Stop with no open prompt whose reply is byte-equal to any reply already kept (result.md, or
any file under results/, or the kept temp file named by laterReplyPath) changes nothing: no archive, no counter change, no path
change.** The Stop hook input carries no message id (only `last_assistant_message`), so bytes are the only identity available; two
different turns that end in byte-identical text carry no new information, so keeping one copy loses nothing. Do the check with one
helper that compares against every kept file (not a list of two places), and use it wherever a no-open-prompt Stop is handled.
`laterReplies` must equal the number of distinct later replies on disk. The open-prompt (active) path is unchanged.

## Attack the rule before you report
Extend the round-2 table: sequences of up to four events after a statused report, drawn from {new later reply X_i, duplicate Stop of
any earlier reply (the report, or any X_j), submit, Stop with empty reply}, × the round-2 prior-state axis values (including
no-hardlinks). The oracle computes the **complete expected state**: the exact multiset of archive contents, `result.md`, `laterReplies`
and `laterReplyPath`, and compares it with the actual one. Print the input space by code, run it on both interpreters, and report
counts at 6535e16 (before) and after. The reviewer's sequence is a row. Keep the rows worth keeping as tests (at least the delayed
duplicate with one and with two intervening replies).

## Report
- Fail-before for each new test against 6535e16.
- `tests.test_claude_report` and `claude plugin test claude/plugins/tmux-pi`, then the brief's module list (ro1-result-kept.md) plus
  tests.test_contract, in all four cells, one after another (not at the same moment): /usr/bin/python3 and /opt/homebrew/bin/python3,
  each outside (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`env TMUX=/tmp/x,1,1
  TP_SESSION=dummy TP_TASK_ID=dummy`), with `TMUX_TMPDIR` set to a fresh `mktemp -d`. Each log starts with `date -u` and
  `git add -A && git write-tree`; each module's time per cell.
- Attack counts before and after, Self-review, the sweep of every place that decides "already kept", deviations.
- Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells left running.
