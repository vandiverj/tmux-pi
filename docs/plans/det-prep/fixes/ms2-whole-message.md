# MS2: a message counts as delivered only when the worker received all of it (Main, 2026-11-09)

## What happened (production issue: "messages to workers not getting sent")
On 2026-11-10 03:34:31Z Main ran `tp-agent message coder-1b8c0465d7 --text "<1089 characters>"`. `messages.jsonl` recorded
`"submitted": true, "attempts": 1`. The worker's transcript holds one user prompt for it, 83 characters long:
`' that worktree. Report both, then end with RESULT_STATUS: complete. [tp:43582fda07]'`. The first ~1000 characters never reached
the worker. The worker said the message looked cut off and stopped (`RESULT_STATUS: incomplete`). A 202-character message sent a
minute later arrived whole. Saved evidence: `/Users/jerrickvandiver/.claude/jobs/6061f696/tmp/ms2-evidence/`
(`messages.jsonl`, the sender's record; `received.jsonl`, the transcript entry). The full sent text is the second-to-last line
of that `messages.jsonl`.

Cause of the false "submitted": `lib/pane_send.send` types the text with one `send-keys -l` and appends the delivery token at the
end. The Claude confirmation (`delivered`) accepts any submit record that holds the token. The token is the last thing typed,
so it survives when the start is lost. The check proves the end arrived, not the message.

Why the start was lost is not known yet. Find it before you fix it: reproduce with a real Claude Code worker on a private tmux
socket (`-L`, TMUX_TMPDIR from `mktemp -d`), sending messages of increasing length (200, 500, 800, 1000, 1500, 3000, 6000
characters, with and without `$`, quotes, backslashes and `!`), and record for each what the transcript's prompt holds. Note the
Claude Code version. If it does not reproduce, say so, and keep the fix below anyway: the check must not pass a partial message.

## Main's decision: one rule
- **Rule:** `submitted: true` means the worker's submitted prompt holds the whole message that was typed: every character of the
  text, in order, with the token. Anything less is not submitted, and the result says what arrived (length received / length
  sent).
- Prove it through the existing submit record: the UserPromptSubmit hook (claude/plugins/tmux-pi/hooks/report.sh) also records,
  per token, a digest and the length of the prompt text before the token (normalized the same way on both sides, one function
  or one pattern shared and kept equal by a test, like TOKEN_PATTERN today). `send` compares them with the text it typed.
  A prompt that holds other text before ours (the box already had text) must still confirm when our whole text is present;
  define the comparison so that case is right and tested.
- A partial delivery is never retyped automatically into a box that may still hold the rest. The CLI reports it as not
  delivered, with what arrived, and exits non-zero, so the sender can resend (for example by pointing the worker at a file).
- If the cause is a length or burst limit, also fix the send so whole messages arrive (for example type in chunks with a pause,
  or use tmux `load-buffer` + `paste-buffer -p`, bracketed paste). Choose by evidence from the reproduction, and state which.
- pi workers use the pane check; state what it proves for a long message, and do not claim more.

## Attack the rule before you report
Input space: message length (short, around each limit you find, very long) × content (plain, `$`, quotes, backslashes, `!` at
the start, newlines, non-ASCII) × box state (empty, holding other text) × harness (claude, pi) × what arrives (all, the end only,
the start only, nothing, twice). Print by code, run each against `send` with a fake hook record where a live worker is not
needed, expected outcome and reason, counts. Keep worth-keeping rows in tests/test_pane_send.py. Live rows run only on a private
socket.

## Report
- The reproduction table with the Claude Code version, and the cause found (or "not reproduced").
- Fail-before against the lane base for the new tests.
- tests.test_pane_send tests.test_claude_report tests.test_main_notify tests.test_session_env and `claude plugin test
  claude/plugins/tmux-pi` in all four cells: /usr/bin/python3 and /opt/homebrew/bin/python3, each outside (`env -u TMUX -u
  TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`env TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
  Each log starts with `date -u` and `git write-tree`. Time per file per cell.
- Lane result-kept (RO1, not merged yet) also edits `report.sh` (the result.md rules). Keep your `report.sh` change to the
  prompt record, in its own function, so the two merge cleanly.
- Never touch the default tmux server. Commit nothing; `git add` new files. `RESULT_STATUS: complete` as the last line.
