# K1 round 2: allowlist of git grep options for the lift (route: rule-first)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-5c2f8af2df-165918/result.md

Round 1 defined "plain" by a denylist, so `-f FILE` and `--textconv` slipped through.

## Rule
The lift applies only when every token of the git grep argv is one of:
- an option on an explicit allowlist (matching flags such as -n, -i, -w, -v, -l, -L, -c, -h, -H, -E, -F, -G, -P, -I, --count, --line-number, -A/-B/-C/--context NUM and -e PATTERN, plus any other output- or match-only flags you justify);
- the pattern;
- the `--` separator;
- a pathspec root.

Any option that reads a file (`-f`, `--file`, `--exclude-from`…), runs or changes a content driver (`--textconv`, `--open-files-in-pager`, `-O`), or widens the source is outside the allowlist and keeps the prompt. Abbreviated long options are resolved the way git resolves them before the check.

## Tests
- A table with rows `-f agent/patterns.token`, `--textconv`, `--textcon` (an abbreviation) and the previous rows.
- A parity test that parses `git grep -h` output (the options git actually knows) and asserts every option not on the allowlist keeps the prompt.

Leave the tracked-credential-name rule unchanged; that decision is with the owner.

## Added by the owner's decision (option 2)
A tracked credential-named file under the root no longer blocks the lift when the working copy is exactly the committed version:
- the file is a regular file, not a symlink, in both HEAD and the working tree;
- the index entry equals HEAD;
- the working-tree content equals the index (`git diff --quiet HEAD -- <file>` through `_git_ask`).

git grep then reads only content already in history. Any difference (unstaged edit, staged edit, type change, symlink, or a git error or timeout) keeps the prompt, as now. Batch the check into one guarded query per root, not one per file.

Owner check (2026-11-07): in this repo the committed `agent/mcp.json` and `claude/mcp.json` hold only `${VAR}` references for credentials, and `bin/tp-secret-exec` holds no token-like strings.

Extra table rows:
- clean tracked `mcp.json` → lift;
- `mcp.json` with an unstaged edit → prompt;
- staged edit → prompt;
- a tracked `mcp.json` replaced on disk by a symlink → prompt;
- the git diff query failing → prompt;
- a clean tracked `*secret*` name → lift.
