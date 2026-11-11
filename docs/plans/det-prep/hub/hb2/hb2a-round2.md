# HB2a round 2: one search rule everywhere, each instruction once (Main, 2026-11-11)

Review review-coder-afcdda022c-071232 (REQUEST_CHANGES): `result.md`, `report.md`, `reproduce.py`, `claude-system-prompt.md`,
`claude-packet.md` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-afcdda022c-071232/`.
The HB2a brief (`hb2a-brief.md`, this dir) still applies. Bar: realistic use. Lane base for fail-before: 5ca1176. Kind of round:
mechanical (Sonnet).

## Main's diagnosis
Worker misses of stated rules: the safe-tool descriptions say more than SG1 enforces (F1, brief's "one read/search rule"); two
directives are still delivered twice to Claude workers (F2, rule 2 "once per rule"). F3 and F4 are notes the review accepts as
disclosed limits. F5 is evidence.

## The rules
1. **The descriptions say what SG1 enforces (F1).** `agent/extensions/safe.ts:29-32` and `claude/mcp/safe.mjs:30-33` (and their
   examples and the assertions on them) state the same rule as `agent/contract/worker.md:4`: safe_grep/safe_read for searches and
   reads in general; a shell `grep`/`rg` only on exact, named, non-sensitive files in the tree. Do not change SG1's enforcement.
   One wording, used in the contract and both descriptions (or the descriptions point to the contract's rule).
2. **Each directive once (F2).** `bin/tp-spawn:542-544` keeps the report path (a fact) and drops the directive the contract already
   gives; the extra Claude "end every final reply with a RESULT_STATUS line" goes where the contract's status rule covers it. Prove
   with the review's fake-tmux Claude launch: each directive appears once in the rendered system prompt and packet, for pi and Claude.
3. **Base typecheck (F5).** Run the same `tsc` command on a clean checkout of 79b2702 (a temporary `git worktree add --detach` under
   your own temp dir, removed after) and keep its log beside the current-tree log, so the pre-existing TS2339 is shown on the base.
4. F4 (no-rg fallback lacks .git/gitignore exclusions) goes into docs/agent-tools.md as a known portability limit, one sentence.

## Tests (tests earn their place)
Extend the existing description assertions (test_pi_safe_tools, test_claude_safe_mcp) and the delivery test in test_orchestration
with a duplicate-directive check; no new test files. Fail-before against 5ca1176. Report added vs extended.

## Verification (the worker tests once)
Run tests.test_pi_safe_tools, tests.test_claude_safe_mcp, tests.test_orchestration, tests.test_preamble, tests.test_tp_review,
tests.test_agent_tools, tests.test_session_env once, with `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION
-u TP_TASK_ID -u TP_STATE_DIR`), under `perl -e 'alarm 900; exec @ARGV'`, the log starting with `date -u`, `TMUX_TMPDIR` and
`git add -A && git write-tree`, then `git reset -q`. Tests needing tmux use a private `-L` socket and TMUX_TMPDIR. Main runs the four
cells and the full discovery. No Claude or pi start; nothing to 127.0.0.1:1234 or any network. Search with safe_grep, read with
Read/safe_read; never read claude/mcp.json, .env* or auth files. Commit nothing. No background shells. `RESULT_STATUS: complete` as
the last line.
