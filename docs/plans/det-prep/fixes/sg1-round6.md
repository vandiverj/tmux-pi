# SG1 round 6: RUNS_CODE only from a positive script-file parse (Main, 2026-11-10)

Review review-coder-14a568610a-164843 (REQUEST_CHANGES), report, probes and sweep:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-14a568610a-164843/`
(probe.py, handlers.py, pi-handlers.mjs, sweep.py, sweep-rows.json). Rounds 1-5 briefs still apply. Bar:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`.

## Why the route is design
Family worker-search-still-asks recurred in every round. Round 5 found inline code by listing its spellings (`-c CODE`, `-e CODE`,
`-` stdin), and the review found spellings outside the list: attached `-c'…'`, `-ne'…'`, clusters `-cx`, `--eval=CODE`, `--` before
`-`, valued options before `-`, and a code literal masking a sensitive env value. Its option scan also ran past the script operand, so
`python3 tools/check.py -c dev config/local.json` (the script's own `-c`) became inline code (F4, a regression). Another list will
miss the next spelling. Invert it.

## The rule
For an interpreter (python, node, perl, ruby, bash/sh/zsh, deno, … every program the RUNS_CODE sites accept), RUNS_CODE is given only
when a **positive parse** shows it runs a script file:
1. Walk the arguments left to right. Each argument before the script operand must be an option on a **closed per-interpreter list**
   of known options that take no code (with its known value arity, e.g. python `-W ignore`, `-X dev`, `-u`, `-B`; node
   `--max-old-space-size=…`). Anything not on the list (any code option in any spelling, an attached value, a cluster, `--eval=`,
   an unknown option) ends the parse with **no cause**.
2. `--` is allowed once; the next argument is the operand.
3. The operand must be a file path that is not `-`, `/dev/stdin`, `/dev/fd/*` or a process substitution, and is not itself sensitive.
   Everything after it belongs to the script and is **never parsed** as interpreter options (closes F4).
4. Then round 4/5 apply: the script file with a sensitive argument keeps RUNS_CODE (known limit).
5. **Inline code** (whatever ends the parse at a code option) keeps round 4's literal rule only when the sensitive name is a literal in
   the code **and** no sensitive name reaches it any other way: an operand, stdin, heredoc, or an env assignment on the command whose
   value names a sensitive path, whatever the variable is called (closes F3 and the weak-env allow row, see F5 below). Otherwise no
   cause.
Apply it in one function used by all RUNS_CODE sites (direct, xargs, find -exec, fd -x, loops); `test_non_search_sites` must show each
site calls it. Main's decisions only ever get stricter.

## Findings
- **F1, F2, F3 (Closes by: code):** every review repro refuses in a worker (Claude shadow, "on", pi) at every site; main still asks.
  Add rows for each spelling family per interpreter, not one example.
- **F4 (Closes by: code):** `python3 tools/check.py -c dev config/local.json` and `node scripts/x.js -e y config/local.json` are back
  to round 4's decision (ask). Add rows where a script's own arguments look like interpreter options.
- **F5 (Closes by: decision):** Deno literal: Main accepted it (matches `node -e '<literal>'`); record that in the table. Weak-name env
  allow (`F=config/local.json python3 -c …`): fixed by rule 5 in this round, so it is no longer allowed in a worker; record any other
  program that still allows a weak-name env value as a known limit with its row.
- **F6 (Closes by: check):** correct the two historical sweep rows (round 1's site is the unrelated-first-search guidance; round 2's
  is the non-search xargs git commands) and cite the right evidence for short `-f` and long `--file`.

## Report
Fail-before against 1b877cf for each new row; the attack extended with the review's sweep.py rows and an input-space table per
interpreter (option before operand: known/unknown/attached/cluster/long `=`; operand: file/-/dev-stdin/`--`; after operand: option-like
script args; env value sensitive/not), each through every site; the four cells as in round 5 (same 10 modules), one after another,
each log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`; per-module times. List every
decision that changed versus 1b877cf, in both directions, and why. Update known limits. Search with safe_grep and read with
Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*`
or auth files. Test strings that name `config/` or `.env` paths go in a script file. Nothing sent to 127.0.0.1:1234 or any network
address; no Claude or pi start. Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells.
