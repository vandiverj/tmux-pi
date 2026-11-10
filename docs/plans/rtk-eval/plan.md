# RTK evaluation: does it cut tmux-pi tokens without losing accuracy?

Owner, 2026-11-08: "I want to reduce tokens but not at the cost of effectiveness and accuracy."
Branch `rtk-eval` (worktree `tmux-pi-wt-rtk-eval`, from lane/integrate `5ea21ba`).
RTK v1.1.0 (full edition: Apache-2.0 + BUSL-1.1 engine) is at `~/.local/rtk-eval/bin/rtk`. It is sha256-verified against the release `checksums.txt`, is not on PATH, and has telemetry off.
`TP_RTK=1` turns it on:
- `bin/tp-claude` adds `rtk hook claude` as a PreToolUse Bash hook beside the tmux-pi plugin hooks.
- `bin/tp-pi` loads `agent/extensions/rtk.ts` last, after the sensitive and kill guards.

Smoke runs, 2026-11-08: Claude Haiku via tp-claude and local Qwen via tp-pi. Both rewrote commands (`rtk gain --history`), and both answered correctly.

## Decision rule (set before measuring)
Adopt only if **all** hold:
1. **Fidelity:** no information loss on the command families tmux-pi agents use. A family is acceptable only if it is lossless or can be excluded through `[hooks] exclude_commands`, with the exclusion recorded.
2. **Guards unchanged:** every trust-guard, sensitive-read, redact, kill-guard and secret-placeholder decision is the same with RTK on, and redaction still applies to RTK output.
3. **Accuracy non-inferior in live A/B:** no task's correctness rate drops with RTK on, and RTK adds no extra turns or bypass reruns that eat its savings.
4. **Savings that matter:** at least a 10% reduction in billed input context (input + cache read + cache write) per task, median across the A/B tasks.

Otherwise don't adopt. Partial adoption, such as only certain commands or only Pi, is allowed if the data supports it.

## Phases
0. **Ceiling.** From real worker transcripts, find what share of context tokens comes from Bash tool results, by command family. This bounds RTK's possible savings. On Claude, tool output stays in context and is re-read from cache every later turn, so weight each result by the turns that follow it.
1. **Replay fidelity.** No LLM is involved. Take about 100 real read-only commands from those transcripts. Run each raw and as `rtk rewrite <cmd>` produces it, in this worktree at the same commit. Measure the byte reduction and check deterministically for loss:
   - unittest: every FAIL/ERROR name, assertion line and `Ran N` kept;
   - git diff/show: every hunk and changed line kept;
   - grep/git grep/rg: set of file:line matches equal;
   - cat/sed -n/head: exact content and line numbers kept.
2. **Guard compatibility.** Drive the real hook chain (Claude) and extension chain (Pi) on fixture commands. Does RTK's `updatedInput` clobber the redact hook's `|| echo exit-code` suffix? Does a rewritten command escape sensitive-read, kill-guard or trust-guard? Does PostToolUse redaction apply to RTK output?
3. **Live A/B.** Paired tasks with known answers, run with RTK on and off. Same commit, model and prompt. At least 3 reps per arm, on Claude Sonnet via tp-claude and on local Qwen via tp-pi, both in worker mode (`TP_TASK_DIR` set). Metrics:
   - tokens: input, cache read, cache write and output;
   - turns, wall time, `rtk bypass` reruns;
   - correctness, scored by a deterministic checker.
