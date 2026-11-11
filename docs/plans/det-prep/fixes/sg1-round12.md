# SG1 round 12: a glob in cd is an unknown directory; a proven script's arguments are not a shell (Main, 2026-11-11)

Review review-coder-fd39102dc0-000538 (REQUEST_CHANGES), report and repros: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-fd39102dc0-000538/`.
Rounds 1-11 briefs still apply. Bar: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`.
Route rule-first; keep the change small.

## Findings
- **F1 (code):** a `cd` operand containing any glob or expansion character (`* ? [ { ~ $` or a backquote) is an unknown
  directory and uses the existing unknown-directory fallback, so `cd conf*; printf '%s\n' 'f=local.json' 'cat "$f"' | bash -eu`
  refuses in a worker (Main asks). `cd docs && cat README.md` and `cd docs/* ` with ordinary files only stay allowed.
- **F2 (code):** the stdin-shell scan must not look inside the operands of a proven project script call (round 6's script
  boundary). `python3 tools/run.py -- bash -eu config/local.json`, the `bash tools/x.sh ...` variants and git pathspecs keep
  their ca6d987/round-10 decision (RUNS_CODE: ask). Add these, and every shape the review names, as guard rows.
- **F3 (check):** correct sweep row 4: round-4 F1's current counterpart is the interpreter branch near
  `lib/sensitive_read.py:2195-2205`.

## Report
Fail-before against e76c046 for each new row. Rerun: the review's 96-row rule-attack, attack11 (1957), the round-10 (11909) and
round-9 (3110) corpora; show every changed decision versus e76c046 with its reason (expected: F1 rows refuse, F2 rows back to their
round-10 decision, nothing else). Add to attack11 the cd-operand shapes (glob, brace, tilde, variable, command substitution) and
script-argument shapes (python3/bash/node project scripts, `--`, git pathspecs). The four cells as in round 11 (same modules), one
after another, in the foreground, each log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then
`git reset -q`; per-module times. Update known limits. Search with safe_grep and read with Read/safe_read; never search `bin/`
as a directory or `config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*` or auth files. Test
strings that name `config/` or `.env` paths go in a script file. Nothing sent to 127.0.0.1:1234 or any network address; no Claude
or pi start. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line, exactly.
