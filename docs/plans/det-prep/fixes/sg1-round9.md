# SG1 round 9: shell source text is checked word by word (Main, 2026-11-10)

Review review-coder-df22b45f7c-202314 (REQUEST_CHANGES), report and repros:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-df22b45f7c-202314/`.
Rounds 1-8 briefs still apply (round 7: inline code never earns RUNS_CODE; round 8: no skipped words in stdin data). Bar:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`. Route rule-first; keep the change small.

## The rule
- **Shell source text is checked word by word, like any other inline code.** For every route where text runs as shell source
  (`bash -c '…'`, `sh -c`, a stdin shell fed by heredoc, here-string or pipe, `bash -s`, and the same under `env`, `xargs`,
  `find -exec` wrappers), each shell word of that source, including the value side of a `NAME=value` assignment and the
  operands of `set`, `export`, `local`, `declare`, is checked for a sensitive name. A hit refuses in a worker; Main asks.
  Keep the existing command parse as well; this check is added, not a replacement.
- No variable-flow analysis: the name must be visible in the source text. Exports in an earlier separate command stay a known limit
  (round 7 F4).
- The positive project-script exception (round 6, `_script_operand`) is unchanged.

## Findings
- **F1 (Closes by: code):** these refuse in a worker and ask in Main (real Claude shadow hook and pi handler, plus the classifier):
  `bash -s <<'SH'` / `set -- -e config/local.json` / `cat "$2"` / `SH`; `bash -s <<< 'set -- -e config/local.json ; cat "$2"'`;
  `bash -c 'set -- -e config/local.json ; cat "$2"'`; and `f=config/local.json` then `cat "$f"` in each of those three routes.
  Claude "on" mode stays as it is for heredoc/here-string text (known limit).
- **F2 (Closes by: check):** correct sweep row 6 in the report: historical lines 2920-2923 concern non-search refusal wording, not target
  resolution (see the review report for the mapping). No product change.
- Find every other place where shell source is parsed as commands without its words being checked (the review traced
  `_document_hit` and the `-c` branch near `_command_hit`); apply the rule there and list them.

## Report
Fail-before against cc4b97a for each new row; the attack extended with the review's repros plus, per shell-source route (`-c`,
heredoc, here-string, pipe into `bash -s`, under `env`/`xargs`/`find -exec`), rows with the sensitive name as a `set --` operand,
an assignment value, an `export`/`local` value, and a plain `echo` operand; plus guard rows that must stay allowed (shell source
naming only ordinary files, and `bash tools/x.sh config/local.json`-style script rows keeping their round-6 decision). List every
decision that changed versus cc4b97a in both directions with the reason. The four cells as in round 8 (same 10 modules), one after
another, in the foreground, each log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`;
per-module times. Update known limits. Search with safe_grep and read with Read/safe_read; never search `bin/` as a directory or
`config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*` or auth files. Test strings that name `config/`
or `.env` paths go in a script file. Nothing sent to 127.0.0.1:1234 or any network address; no Claude or pi start. Commit nothing. No
background shells. `RESULT_STATUS: complete` as the last line, exactly.
