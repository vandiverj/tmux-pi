# SG1: workers search and read through the safe tools from the start (Main, 2026-11-10)

Owner production issue (work Mac, 2026-11-10): "the workers do not use safe_grep/read from the beginning and main does not tell
them to … a lot of workers are trying - grep -C until I told main to have them use safe commands". The owner asked for all three
fixes below: "go ahead with 1 and 2, and 3 too".

## What Main found (check each, with file:line, before you change anything)
- Workers get no search rule. `lib/agent_tools.py:41-46` STANDING names `safe_read` but not `safe_grep`, and the block is printed
  only when `.tmux-pi/agent-tools.json` exists (neither this repo nor most work repos have one).
- `agent/roles/coder.md:21` tells coders to run `grep -rn SYMBOL tests/`.
- Only reviewers get a rule: `bin/tp-review:244` (`outside_rule`).
- `Bash(grep:*)` is allowed in `claude/settings.json`; the stop comes from the sensitive-read PreToolUse hook (`lib/redact_hook.py`,
  searchers at :384-486) asking on a Bash search. Its messages point to `safe_read` only for some file kinds.
- Claude workers have `mcp__tmux-pi-safe__safe_grep`/`safe_read` (claude/mcp/safe.mjs); this session's coder transcripts call
  them about 2 times each against 4-56 Bash greps. Pi has `safe_read`/`safe_grep` in `agent/extensions/safe.ts`; check that a pi
  *worker* (not only main, `bin/tp-pi:20` MAIN_TOOLS) actually has them, and say what you find.

## Fix (all three, owner-approved)
1. **Standing rule in every worker's system prompt**, both harnesses, whether or not a tools manifest exists (`bin/tp-spawn`'s
   system-prompt assembly, or `lib/agent_tools.py` printed unconditionally; choose one place and say why). Text, short: search with
   safe_grep, read with safe_read or the Read tool on an exact path; Bash grep/rg only on exact worktree paths known not to be
   sensitive. Use each harness's real tool names. Replace the `grep -rn` example in `agent/roles/coder.md:21` and any other role
   text that tells a worker to search through Bash (list every one you find, by code, in agent/roles/*.md).
2. **The hook's message names the safe tool.** When the hook would ask (or deny) on a Bash search, its reason names safe_grep with
   the same pattern and path (Claude: `mcp__tmux-pi-safe__safe_grep`; pi: `safe_grep`), so the model can switch on the first hit.
   Same for a Bash read of a file (cat/head/sed -n/tail) that triggers the ask: name safe_read.
3. **Workers are refused, not asked.** In a worker task (`TP_TASK_DIR` set; confirm that is how the hook and `sensitive.ts` know)
   a Bash search or read that would ask for a sensitive read is denied with the message from fix 2, so no worker waits on a prompt
   nobody answers. Main and the owner's own sessions keep the ask exactly as now. Never turn an ask into an allow; never weaken a
   deny. The trust-store guard, the `.env` rules, placeholder substitution and `reveal` are unchanged. Record each refusal where
   the hook already records permission requests, so Main can see it.

## Attack (realistic-use bar, docs/plans/realistic-use-bar.md)
Build the input space by code and print it: the Bash search and read forms workers really used (take them from this repo's
worker transcripts under `.tmux-pi/sessions/*/tasks/*/` — grep -C/-n/-rn, rg, git grep, cat/head/sed -n/tail, with pipes) × target
(exact safe worktree file, protected dir agent/ config/ claude/, a `.env`-like decoy you create in a temp dir, a path outside the
worktree) × session kind (worker with TP_TASK_DIR, main without) × harness (Claude hook, pi extension). Expected, per row: the
decision before and after (allow / ask / deny), and after: worker rows that asked now deny with a message naming the safe tool;
main rows unchanged; no allow row changed; no deny row weakened. Report counts before and after.

## Report
- Fail-before against the lane base for each new test; tests for the prompt text (both harnesses), the hook decision table and the
  pi extension.
- tests.test_session_env, the hook and redaction tests (find them), tests.test_tp_spawn* / agent-tools tests you touch, and any pi
  extension test, in all four cells (/usr/bin/python3 and /opt/homebrew/bin/python3, outside tmux with
  `env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR` and inside with `TMUX=/tmp/x,1,1 TP_SESSION=dummy
  TP_TASK_ID=dummy`), one after another; each log starts with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then
  `git reset -q`; each module's time per cell; respect SLOW_TESTS.
- Attack table, sweep of every place each rule applies (file:line), Self-review, shared files changed, deviations.
- While you work, search with safe_grep and read with Read/safe_read yourself. Never read `.env*` or auth files; never search
  `bin/` as a directory or `config/`, `agent/`, `claude/` recursively — name the files. Do not edit the Claude profile or trust
  store. Any test that starts tmux uses a fresh `TMUX_TMPDIR` and a private `-L` socket. Nothing sent to 127.0.0.1:1234 or any
  network address; no Claude or pi start. `git add` new files. Commit nothing. `RESULT_STATUS: complete` as the last line, exactly.
  No background shells left running.
