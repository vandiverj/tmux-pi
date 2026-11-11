# PF1a: where a tp-review run and a tp-agent command spend their time (measure only) (Main, 2026-11-11)

Owner priority: test speed (and real review/dispatch speed). `q4-findings.md` (this dir) found the cost is inside tmux-pi's own
commands: one `bin/tp-review` run ~1.47 s with ~18 python3 processes plus tp-agent and jq; a full-run trial test spends ~6 s in six
`tp-agent` calls at ~1 s each while `tp-agent --help` takes 0.06 s. Main's decision (owner delegated, 2026-11-11): a profiling lane
PF1; this task (PF1a) only measures and proposes. **No product code changes; nothing committed.** PF1b (the change) is briefed from
your numbers after SR1 merges.

## Measure
1. Build a scratch tree the way `tests/test_tp_review.py` and the Q4b trial tests do (reuse their fixture helpers; read them),
   in a temp dir under /tmp, private tmux `-L` socket, `TMUX_TMPDIR` fresh.
2. **tp-review:** one plain run and one 200-file run, each 5 times. Record every child process (command line, start offset,
   wall time) with a wrapper on PATH or `python3 -X importtime` where useful; total per process kind (python3 -c snippets,
   tp-agent subcommands, jq, git). Name the source line that starts each.
3. **tp-agent:** the six subcommands a Q4b full-run trial test calls (find them), each 5 times: wall, `python3 -X importtime`
   top 15 imports, `cProfile` top 20 cumulative (sorted), and the git/tmux/subprocess calls each makes.
4. Table: command, median wall, what dominates (import, git calls, file scans, tmux calls, sleeps/polls), source lines.

## Propose (do not implement)
For each cost over 0.1 s: the smallest change that removes it, its risk (what behaviour could change), which tests prove it, and
the expected saving per test and per full run (5401 tests, ~586 s parallel). Rank by saving / risk. Flag anything that is a
sleep or poll.

## Rules
Read-only on the repo: writes only under /tmp and your task dir. `/usr/bin/python3`; outside tmux (`env -u TMUX -u TMUX_PANE -u
TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`). No Claude or pi start; nothing to 127.0.0.1:1234 or any network; never
the default tmux server. Never run tp-agent against this repository's real session or lanes (scratch state dir only). Search with
safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. A guard refusal of a
shell command: rephrase with a script file under your task dir if the command was legitimate, list it in the report; any other
refusal: stop and report. Approve no prompt. No background shells. Report in report.md (tables, commands used, raw data under
evidence/). `RESULT_STATUS: complete` as the last line.
