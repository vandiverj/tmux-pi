# PF1b round 2: the whitespace test is equivalent for every byte string (Main, 2026-11-11)

The PF1b brief (`pf1b-brief.md`, this dir) still applies. Kind of round: mechanical (Sonnet). Lane base for fail-before: 6a04098.

## Review F1 (review-coder-0087076a36-172322)
Under bash 3.2 and `LC_ALL=en_US.UTF-8`, a task in a legacy encoding (reviewer: "Исправь ошибку" as CP1251) was nonblank to
the old `${task//[[:space:]]/}` test and blank to the new `=~ [^[:space:]]`: tp-spawn exits 1 with Usage. Under `LC_ALL=C`
both accept. Same at bin/tp-review:146. Reviewer's probe: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-0087076a36-172322/invalid_bytes.py. **Diagnosis:** the brief said
"behaviour-preserving" but its attack list named characters, not bytes; a character regex does not see malformed bytes.

## The rule
At both sites the new predicate answers exactly as the old one for **every byte string** in UTF-8 and C locales, and stays
linear. A suggested shape (verify, do not assume): blank only if the regex finds no non-whitespace character *and* the string
holds no byte that is not part of a whitespace character (e.g. a second regex check under a C-locale comparison, or a
`case` glob), whichever your oracle shows equivalent. One shared oracle script compares old vs new for both sites over: the
round-1 inputs, invalid UTF-8 (lone continuation bytes, truncated sequences, CP1251 and Latin-1 text, 0xFF), invalid bytes mixed
with whitespace, and whitespace-only strings of 10, 1000 and 4000 chars, under both locales; it prints one line per case with
old and new answers. Extend the existing tp-spawn test (and tp-review's approval test) with invalid-byte rows.

## Evidence owed (review F2)
- Count `git rev-parse --git-common-dir` calls in one ordinary `tp-agent dispatch` on base and tree (a PATH shim that logs
  argv to a file is fine), with the paths asked. Once per path is the accepted design; report the numbers honestly.
- Per-case oracle output (above), not aggregates.
- Timing: old vs new on a 4000-char task and on a whitespace-only 10 000-char string, each under `perl -e 'alarm 60; ...'`
  (a timeout is a valid "old > 60 s" answer). No unbounded runs.
- compileall: you reverted it; state the two times you measured. No new logs needed.

## Review F3 (decision, Main)
Killing your own process by PID after `pkill` was refused was a departure: any refusal other than an edit means stop and
report. Main accepts it as done (no damage shown). In the report, state from your session what that PID was (command line)
if you still can, else say it is not established. Do not repeat it.

## Verification (the worker tests once)
Run once, `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u
TP_STATE_DIR`) through `tools/agent/test-run.sh`, fresh short `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 600; exec @ARGV'`:
the test modules you extend plus tests.test_session_env. Main runs the cells and discovery. Write report.md in the task dir.
No Claude or pi start; nothing to 127.0.0.1:1234 or any network; never the default tmux server. Edit only bin/tp-spawn,
bin/tp-review, the tests you extend and evidence files in the task dir. Search with safe_grep, read with Read/safe_read; never
read claude/mcp.json, .env*, auth files or config/local.json. Guard refusals: if a guard hook refuses a shell or inline-script
edit, make the same edit with Edit/Write and list it in the report; any other refusal means stop and report. Do not approve
any permission or placeholder prompt. Commit nothing. `RESULT_STATUS: complete` last.
