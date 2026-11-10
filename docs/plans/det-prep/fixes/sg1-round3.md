# SG1 round 3: wrapped searches refused, git tree/index searches generic, xargs git non-search keeps its ask (Main, 2026-11-10)

Review review-coder-153677e451-113620 (REQUEST_CHANGES), report, reproducers and logs:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-153677e451-113620/`
(`report.md`, `reproduce.py`/`reproduce.log`, `pi-reproduce.mjs`/`pi-reproduce.log`, `probe.py`/`probe.log`).
`sg1-round2.md` still applies; this brief adds to it. Keep everything rounds 1-2 built and the review confirmed (batches refused,
absolute targets, every search named, main sessions unchanged, never weaker, `refused` never treated as waiting). Bar:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`. F1-F3 are realistic.

## The rule change (route rule-first; this family recurred across rounds 1-2)
The refuse-or-ask decision must come from **the classifier's own identification** of a shell search or read (the reason it
asked: it already recognises `sh -c 'grep …'` and `xargs sed -n`), not from a second list of forms in `safe_route`. Make the
classifier return that cause in its result (e.g. a `search_read` flag or the kind of finding), and have every consumer refuse a
worker when that cause is present. `safe_route`'s parser then only decides which safe calls to name; when it cannot name them
faithfully, the message is generic. Asks with any other cause keep their ask. Show by code that every classifier path that
identifies a shell search/read sets the cause (enumerate them), and that no other path does.

## F1 (Closes by: code): wrapped and piped-filename searches are refused too
- `sh -c '…'`, `bash -c '…'`, `bash -lc '…'`, `zsh -c '…'` (and the same with `-e`/`-x`-style flags before `-c`): parse the
  single-quoted or double-quoted script and route it as if it were the command (worker refusal, safe calls named where faithful,
  generic otherwise). A wrapper whose script holds no shell search/read keeps its ask. Unparseable script text → generic refusal
  only if the classifier identified a search/read in it; otherwise the ask stands.
- `… | xargs sed -n …` (and the other readers already recognised after xargs: cat/head/tail/grep family) → refused, generic.

## F2 (Closes by: code): git searches outside the worktree get generic guidance
`git grep` with a revision anywhere among its operands (`git grep TOKEN HEAD`, `git grep -e TOKEN main`), with `--cached`, or with
`--no-index` paths you cannot map one-to-one, names no path: the worker is still refused, with the generic message. Decide a
revision by code where you can (`git rev-parse --verify --quiet ARG^{commit}` in the event cwd with a short timeout, or a
conservative rule: an operand that is not an existing path in the resolved directory is not named). Never suggest a path that
does not exist or a worktree file when git searched the index or a tree.

## F3 (Closes by: code): xargs running a non-search program keeps its ask
`xargs git SUBCOMMAND` counts as a search only for `git grep` (and `git show`/`git cat-file` only if the round-2 rules already
treat them as reads; say which). `xargs git diff`, `xargs git log`, `xargs git blame` and other non-search subcommands keep the
classifier's ask in workers (record `pending`), as round 2's F1 required. Sweep the other xargs inner programs the same way.

## Attack (realistic-use bar)
Extend round 2's generator (`evidence/attack2.py` from coder-153677e451) by code: add the reviewer's repro forms (all three
findings), `sh/bash -c` around each round-2 form that is a search, `bash -c` around a non-search (python, git diff), `xargs sed -n`,
`xargs git diff/log/blame/grep`, `git grep PATTERN REV`, `git grep --cached`, `git grep PATTERN REV -- PATH`. Per row: decision before
(1f3f846) and after, record status, and for worker refusals the named calls checked against the shell's real target (generic
expected for tree/index searches). Counts. No ask→allow, no deny weakened, main rows unchanged, no non-search ask turned into a refusal.

## Report
Fail-before against 1f3f846 for each new test row; the four cells (`/usr/bin/python3` and `/opt/homebrew/bin/python3`, outside and
inside tmux), one after another, with tests.test_session_env, tests.test_sensitive_read, tests.test_sensitive_trust,
tests.test_trust_guard, tests.test_claude_redact_hook, tests.test_redact, tests.test_pi_redaction, tests.test_pi_safe_tools,
tests.test_orchestration, tests.test_worker_env; each log starts with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`,
then `git reset -q`; per-module times. Update the known-limits list (sh -c is no longer one). Search with safe_grep and read with
Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`,
`.env*` or auth files. Test strings that name `config/` or `.env` paths go in a script file, not shell text. Nothing sent to
127.0.0.1:1234 or any network address; no Claude or pi start. `git add` new files. Commit nothing. `RESULT_STATUS: complete` as
the last line, exactly. No background shells.
