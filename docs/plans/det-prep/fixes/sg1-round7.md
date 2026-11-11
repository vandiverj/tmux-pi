# SG1 round 7: inline code never earns RUNS_CODE (Main, 2026-11-10)

Review review-coder-3360958663-180443 (REQUEST_CHANGES), report and repros:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-3360958663-180443/`.
Round 6's positive script-file parse stands. Rounds 1-6 briefs still apply. Bar:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`.

## Why (route rule-first)
F1-F4 all go through one opening: round 4's rule that inline code naming a sensitive file **only as a literal** keeps RUNS_CODE
(worker asks). Each round finds a new way to hide a real read behind that literal (a repeated `-c` operand, an attached second `-e`, a
here-string or heredoc filename, a shell startup option such as `--rcfile .env` around a literal-only nested command). Inline code
that touches a sensitive file is a read in any case: a worker should use safe_read or ask Main. So remove the rule.

## The rule
- **Inline code** (whatever ends round 6's parse at a code option, in any spelling, any count, at any nesting depth, including stdin
  code, here-strings and heredocs) **never** gets RUNS_CODE. If the command touches a sensitive name anywhere (literal in the code,
  operand, option value, stdin, here-string, heredoc body, env value), it has no cause → refused in a worker. Main is unchanged (asks).
- Only round 6's positive script-file parse gives RUNS_CODE. A shell given `-c` is inline code: the shell `-c` branch
  (`lib/sensitive_read.py` ~2177-2184, ~2947-2965) goes through the same parse, and a sensitive value of any shell startup option
  (`--rcfile`, `--init-file`, `-o`, `BASH_ENV=`/`ENV=` assignments) ends it with no cause.
- The Deno literal row and round 6's 6 literal-only attached/`--eval=` rows become refused in a worker. That supersedes Main's
  earlier Deno decision; record it.
- Delete the literal-rule code, not just bypass it; `test_non_search_sites` must show no RUNS_CODE site accepts inline code.

## Findings
- **F1-F4 (Closes by: code):** each review repro refuses in a worker (Claude shadow, "on", pi) at every site; main asks. Add an
  input-space row set for inline code: literal only / operand / repeated code option (separate, attached, cluster) / here-string /
  heredoc filename / env value / shell startup option, per interpreter, through every site.
- Keep: `python3 tools/check.py -c dev config/local.json` and `node scripts/x.js -e y config/local.json` ask (round 6 F4); script
  runs with normal options (`python3 -u`, `-B`, `-m pytest`, `node --test`, `bash -e script.sh`) keep round 6's decisions.
- **F5 (Closes by: check):** label counts as unique commands vs row pairs (the review counts 447 row pairs, 1,766 unique commands of
  1,775 rows), reconcile the "948 fixed" total, record the weak-env row as decided by Main (known limit: project code given an env
  value), and complete the slow-test accounting.

## Report
Fail-before against 6cb3f41 for each new row; the attack extended with the review's repros and the input-space rows above; list every
decision that changed versus 6cb3f41 in both directions with the reason (expected: ask→refuse only, for inline code in workers). The
four cells as in round 6 (same 10 modules), one after another, each log starting with `date -u`, `TMUX_TMPDIR` and
`git add -A && git write-tree`, then `git reset -q`; per-module times. Update known limits. Search with safe_grep and read with
Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*`
or auth files. Test strings that name `config/` or `.env` paths go in a script file. Nothing sent to 127.0.0.1:1234 or any network
address; no Claude or pi start. Commit nothing. Run the cells in the foreground, no background shells. `RESULT_STATUS: complete` as
the last line, exactly.
