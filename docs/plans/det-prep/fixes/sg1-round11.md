# SG1 round 11: the stdin-shell line rule uses the parser's existing context (Main, 2026-11-10)

Review review-coder-ea8e9fabc5-230043 (REQUEST_CHANGES), report and repros: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-ea8e9fabc5-230043/`.
Rounds 1-10 briefs still apply; round 10's rule is unchanged. Bar: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`.
Route rule-first; keep the change small.

## Main's judgement
All three findings are places where round 10's whole-line check does not reuse context that the classifier **already**
tracks elsewhere. Each fix is a reuse of existing code, not a new enumeration. Do not add new tables of programs or spellings.

## Findings
- **F1 (code):** `_stdin_line_hit` judges every piece against a fixed `base=""`. Carry the directory the existing
  traversal keeps (`lib/sensitive_read.py` near :2919-2932) through `cd` and `( cd ...; ... )`, so
  `cd config; printf '%s\n' 'f=local.json' 'cat "$f"' | bash -eu` and its subshell form refuse. Where the directory cannot be
  known (`cd "$x"`, `cd -`), fail closed: check each piece as if under any directory (a bare sensitive base name hits).
- **F2 (code):** `_has_stdin_shell` must follow the same dispatch routes `_other_command_hit` already follows
  (`_FRONT_ENDS`, `_RUN_WORDS`, near :2712-2717), so `uv run --offline --no-project bash -eu <<< 'f=config/local.json; cat "$f"'`
  and the `poetry run` / `strace` forms refuse. Use the existing tables; do not widen them.
- **F3 (code):** `_has_stdin_shell` must also traverse substitutions stored in `cmd["heredocs"]` for expanding heredocs, so
  `cat <<EOF` / `$(... | bash -eu)` / `EOF` refuses. A quoted delimiter (`<<'EOF'`) does not execute and stays allowed.
- All on the classifier, the real Claude shadow hook and the pi handler, as in round 10. Refuse in a worker; Main asks.

## Report
Fail-before against ca6d987 for each repro row. Add the review's 96-row `rule-attack.py` (in the review dir) to the attack:
all 96 must match; then rerun the full round-10 attack corpus and show 0 loosening and every changed row with its reason.
Guards that must stay allowed: the same shapes naming only ordinary files, `cd docs && cat README.md`, `uv run pytest`,
quoted-delimiter heredocs, `bash -s < tools/x.sh`. The four cells as in round 10 (same modules), one after another, in the
foreground, each log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`;
per-module times. Update known limits (remove limit 5 if F1 closes it). Search with safe_grep and read with Read/safe_read;
never search `bin/` as a directory or `config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*`
or auth files. Test strings that name `config/` or `.env` paths go in a script file. Do not run `uv`, `poetry` or `strace`
for real; classify only. Nothing sent to 127.0.0.1:1234 or any network address; no Claude or pi start. Commit nothing. No
background shells. `RESULT_STATUS: complete` as the last line, exactly.
