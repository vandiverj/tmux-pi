# SG1 round 8: no skipped words in stdin routes; startup env only before the program (Main, 2026-11-10)

Review review-coder-d80a26ee90-191331 (REQUEST_CHANGES), report and repros:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-d80a26ee90-191331/`.
Round 7's rule stands (inline code never earns RUNS_CODE). Rounds 1-7 briefs still apply. Bar:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`. Route rule-first; keep the change small.

## The rule
- **Data that reaches code is data, not options.** Words given to a stdin shell (`bash -s -- …`, `sh -s …`) and the text of
  here-strings and heredocs are checked word by word for sensitive names. No word is skipped because it follows `-e`, `-f`, `--` or
  any other option-like word; options belong to the program's own command line, not to its data.
- **Startup env is only an assignment before the program** (`ENV=… bash -c`, `env BASH_ENV=… sh -c`) or a shell startup option.
  A `NAME=value` word after a script operand is the script's argument (round 6 rule 3: never parsed).

## Findings
- **F1 (Closes by: code):** `bash -s -- -e config/local.json <<'EOF' … EOF` refuses in a worker; main asks.
- **F2 (Closes by: code):** inline code fed `<<< '-e config/local.json'` (and a heredoc with the same text) refuses in a worker; main
  asks.
- **F3 (Closes by: code):** `bash tools/x.sh ENV=.env` and `python3 tools/x.py BASH_ENV=.env` return to round 6's decision (ask; script
  file keeps RUNS_CODE). `ENV=.env bash -c 'x'` stays refused.
- **F4 (note):** exports in an earlier command are not followed into later inline code. Record it as a known limit (follow-up); do not
  refuse ordinary exports.
- Find the other places with the same skip (any helper that drops the word after an option-like word while scanning data) and apply
  the rule there too; list them.

## Report
Fail-before against b257568 for each new row; the attack extended with the review's repros plus, per stdin route (stdin shell args,
here-string, heredoc) and per option-like word (`-e`, `-f`, `--`, `-x`, `--file=`), a row with a sensitive word after it; and
`NAME=value` rows before/after the program for each interpreter. List every decision that changed versus b257568 in both directions
with the reason. The four cells as in round 7 (same 10 modules), one after another, in the foreground, each log starting with
`date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`; per-module times. Update known limits. Search with
safe_grep and read with Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/` recursively; never read
`claude/mcp.json`, `.env*` or auth files. Test strings that name `config/` or `.env` paths go in a script file. Nothing sent to
127.0.0.1:1234 or any network address; no Claude or pi start. Commit nothing. No background shells. `RESULT_STATUS: complete` as the
last line, exactly.
