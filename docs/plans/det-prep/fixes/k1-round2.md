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
