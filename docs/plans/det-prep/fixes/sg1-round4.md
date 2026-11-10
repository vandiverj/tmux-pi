# SG1 round 4: invert the default — a worker's sensitive Bash ask is refused unless its cause is a known non-search (Main, 2026-11-10)

Review review-coder-51c710068c-131921 (REQUEST_CHANGES), report, reproducers and logs:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-51c710068c-131921/`
(`report.md`, `reproduce.py`/`.log`, `targets.py`/`.log`, `pi-reproduce.mjs`/`.log`, `probe.py`/`.log`, `matrix.py`/`.log`).
Rounds 1-3 briefs still apply where this one does not change them. Bar:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`. F1-F3 are realistic.

## Why a design change (route: design)
Family worker-search-still-asks has now recurred in three reviews. Each round marked more search sites positively
(`search_read`), and each review found another classifier path that identifies a search but leaves the mark off
(`grep -f FILE`, `xargs sh -c 'grep …'`, heredoc `$(grep …)`). A positive mark fails open into a stuck prompt.

## The design
Invert the mark. In a **worker**, a Bash command the classifier asks on is **refused** (record `refused`) **unless** the
classifier gives a known **non-search cause**. The non-search causes are a short closed list, set at the few sites that
decide them, for example:
- a runner executing code (python/node/ruby/perl/sh with a script file, `-c` code that runs no search/read);
- `find -exec` / `fd -x` / xargs running a non-search program (including `xargs git diff/log/blame`);
- remote/container commands (ssh, docker exec …);
- encodings/transforms whose input is not a file read the classifier identified (keep round 1's base64 decision);
- git patch output (`git diff`, `git show`, `git log -p`).
Enumerate the list by code from the classifier (each site that sets it) and keep it closed: a new classifier path with
no non-search cause refuses by default. Read/Grep tool asks keep their ask (unchanged). Main sessions unchanged. Every
consumer (Claude shadow hook, "on" redact hook, pi sensitive.ts) applies the same rule. `safe_route`'s parser still only
decides which safe calls to name; when it cannot name them faithfully the message is generic.

Keep `search_read` if it is useful for naming calls, but the refuse/ask decision must not depend on it.

## F1-F3 (Closes by: code)
- F1: `grep -f .env README.md` (with and without `| base64`) → refused in a worker (generic or named). Regression row
  against 1f3f846 behaviour.
- F2: `printf … | xargs -I{} sh -c 'grep -n TOKEN {}'`, `xargs sh -c 'grep -rn TOKEN config/'`, `cat <<EOF … $(grep TOKEN ".env") … EOF`
  → refused (generic guidance is enough).
- F3: a named path is the physical target. `..` after a symlinked component (`link/../config/local.json`, `cd link && grep
  ../config/…`, `git -C link/.. grep …`) resolves as the kernel/git would (resolve the prefix with realpath before applying
  `..`), or the message is generic. Never name a different existing file.

## Evidence (F5, report checks)
- SLOW_TESTS: list every module/test over its budget as the logs show (the pi module ran 7.0 s in one cell).
- The attack: wrap **every** round-2 search form in both `sh -c` and `bash -c` (not a rotation), and add the review's F1-F3
  rows and a symlink fixture whose target oracle resolves physically. Keep the historical "10 mismatches" claim only with
  its retained log; otherwise narrow it.
- Add rows proving the inversion: for each non-search cause, a worker ask stays an ask; for a sample of every other
  classifier reason (enumerated by code), a worker ask is refused. No ask→allow, no deny weakened, main rows unchanged.

## Report
Fail-before against 6a9d0ed for each new row; the four cells (`/usr/bin/python3` and `/opt/homebrew/bin/python3`, outside and
inside tmux), one after another, with tests.test_session_env, tests.test_sensitive_read, tests.test_sensitive_trust,
tests.test_trust_guard, tests.test_claude_redact_hook, tests.test_redact, tests.test_pi_redaction, tests.test_pi_safe_tools,
tests.test_orchestration, tests.test_worker_env; each log starts with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`,
then `git reset -q`; per-module times. Update known limits. Search with safe_grep and read with Read/safe_read; never search `bin/` as
a directory or `config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*` or auth files. Test strings that
name `config/` or `.env` paths go in a script file, not shell text. Nothing sent to 127.0.0.1:1234 or any network address; no Claude
or pi start. `git add` new files. Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells.
