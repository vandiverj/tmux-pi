# Fix brief: e1 round 2

The independent review of your step asked for changes. Its findings are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-d6ff23c1d0-153709/result.md` (full reasoning in `report.md` beside it). Read both first.

1. Fix every finding marked `Closes by: code`. Each fix gets a test that fails without it.
2. Findings marked `Closes by: none` are notes: fix them if the fix is a line or two of docs or tests; otherwise say in
   your report why not.
3. For each `Closes by: check` item, supply the missing evidence in your report (exact command, test count, seconds,
   log path).
4. Do not widen the change beyond the brief and these findings. Do not commit.
5. Your report lists each finding id with what you changed (file:line) and the test that covers it, then the exact test
   command(s) you ran with their pass counts and seconds.
6. Bash: tmux-pi's own redaction hook (lib/redact_hook.py) appends ` || echo exit-code: nonzero` to Bash commands. That is expected; run the commands normally. You must run the tests yourself this round.
