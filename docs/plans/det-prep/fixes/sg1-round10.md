# SG1 round 10: a stdin shell fails closed on any sensitive name in its line (Main, 2026-11-10)

Review review-coder-2bde9a72ff-213750 (REQUEST_CHANGES), report and repros:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-2bde9a72ff-213750/`.
Rounds 1-9 briefs still apply. Bar: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`.
Route rule-first; keep the change small.

## Why the rule changes shape
Rounds 8 and 9 each added a route (stdin data, then shell source) and each review found more spellings of the same thing:
option clusters, a wrapper's stdin, grouped producers, a later line after a script exception. Enumerating how text reaches a
stdin shell does not converge. This round makes the decision fail closed (queue idea H26): stop proving what reaches the shell,
and look at the whole line instead.

## The rule
- **A line that runs a stdin shell** is any command line in which a shell (`bash`, `sh`, `dash`, `zsh`, `ksh`, with **any**
  options or option clusters, `-s` or not, `-c` excluded since round 9 covers it) or `.`/`source` of `/dev/stdin`, `/dev/fd/*`
  or a process substitution, runs with no script-file operand, directly or under any wrapper (`env`, `xargs`, `find -exec`,
  `nice`, `nohup`, `timeout`, `command`, `exec`, ...). When the parser cannot tell whether a shell has a script-file operand, it
  is a stdin shell.
- In such a line, **every word anywhere in the line** (every command of every pipeline stage, group `{ ...; }`, subshell
  `( ... )`, here-string, heredoc body, command and process substitution, plus each piece of a word split at blanks and shell
  operators as in round 9) is checked for a sensitive name. Any hit refuses in a worker; Main asks.
- **The round-6 script exception is per occurrence, never per line.** A sensitive name is exempt only where it is itself the
  proven operand of a project script call (`bash tools/x.sh FILE`, `python3 tools/x.py FILE`) and that call is the whole of the
  text the shell receives. Any other occurrence in the line still refuses (closes F4: no first-hit stop).
- Lines without a stdin shell keep their round 9 decisions. No variable-flow analysis; known limits stay as in round 9.

## Findings
- **F1 (code):** `printf '%s\n' 'f=config/local.json' 'cat "$f"' | bash -eu`, the same into `bash -se` and `sh -eu`: refuse.
- **F2 (code):** `find tools -name x.sh -exec bash -s \; <<< 'f=config/local.json; cat "$f"'` and its heredoc and `set --`
  variants: refuse.
- **F3 (code):** `{ printf '%s\n' 'f=config/local.json'; printf '%s\n' 'cat "$f"'; } | bash -s`, the `( ... )` subshell form,
  and a later pipeline stage that supplies the text: refuse.
- **F4 (code):** `printf '%s\n' 'bash tools/x.sh config/local.json' 'f=config/local.json; cat "$f"' | bash -s` and its
  process-substitution form: refuse; the single-line `echo 'bash tools/x.sh config/local.json' | bash -s` keeps round 9's
  RUNS_CODE.
- **F5 (check):** correct the report's fail-before breakdown to 17 newly refused rows, 7 already-refused guards and 3 changed kept
  rows (total 20 unchanged), and sweep row 10's current counterpart (historic `:2184` is round 7 F1's stdin-argument scan, now
  `:2189`; see the review report).
- All on the classifier, the real Claude shadow hook and the pi handler, as in round 9.

## Report
Fail-before against 4ebc921 for each new row. Extend the attack with the review's repros plus, for each stdin-shell form (plain,
option clusters `-e -u -x -eu -se -xe -o pipefail`, under each wrapper, `.`/`source` of `/dev/stdin`, `/dev/fd/0` and `<(...)`),
the sensitive name placed in each producer shape (first stage, later stage, `{}` group, `()` subshell, here-string, heredoc,
command substitution) and after a script line. Guards that must stay allowed: the same shapes naming only ordinary files, a
stdin shell fed by a plain file (`bash -s < tools/x.sh`), and lines with no stdin shell at all (`cat README.md | grep x`). Every
decision that changed versus 4ebc921 in both directions with the reason, and a separate count of rows that went allow or keep
to refuse only because of the fail-closed parse (no script operand proven). The four cells as in round 9 (same 10 modules), one
after another, in the foreground, each log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then
`git reset -q`; per-module times. Update known limits. Search with safe_grep and read with Read/safe_read; never search `bin/`
as a directory or `config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*` or auth files. Test strings
that name `config/` or `.env` paths go in a script file. Nothing sent to 127.0.0.1:1234 or any network address; no Claude or pi
start. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line, exactly.
