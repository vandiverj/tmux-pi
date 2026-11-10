# SG1 round 2: refuse every worker shell search/read, name the right safe call (Main, 2026-11-10)

Review review-coder-655a0e0bf5-104043 (REQUEST_CHANGES), report, reproducers and logs:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-655a0e0bf5-104043/`
(`report.md`, `reproduce.py`/`repros.log`, `hook-reproduce.py`/`hook-repros.log`, `pi-reproduce.mjs`/`pi-repros.log`).
The lane now contains main (Main merged main 54c6f7f into it; resolution in `bin/tp-sensitive-read` keeps main's call/agent_id
record). Keep everything round 1 built and the review confirmed: prompt rule in both harnesses, never weaker, main sessions keep the
ask, `refused` is never treated as waiting. Bar: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`.
F1-F3 are realistic.

## F1 (Closes by: code): a batch does not undo the refusal
In a worker task, when the classifier asks on a Bash command because of a shell search or read it identified (grep/egrep/fgrep/rg/
git grep/cat/head/tail/sed -n, including `-f` pattern files), the answer is deny, even when the command also holds other commands
(`; ls tests`, `&& git status --short`, pipes) or the search cannot be turned into one exact safe call. The message names the safe
call for each sensitive search/read it can name faithfully; otherwise it names the safe tools generically (both harnesses' real
names) and says to run the other commands separately. Asks that are not a shell search or read (python, base64, find -exec, xargs
running something else, Read/Grep tools) keep their ask, as round 1 decided. Main sessions unchanged. Same in the Claude shadow
hook, the "on" redact hook and pi's sensitive.ts. Record `refused` as now.

## F2 (Closes by: code): the suggested target is the one the shell would search
- `git -C DIR grep ... -- PATH`: the path is DIR/PATH (or DIR), resolved as git would.
- `grep PATTERN < FILE` (and `cat FILE | grep`): path FILE.
- Shell globs (`config/*.json`): use safe_grep's `glob` parameter with the directory as path, or list the matched files; never a
  literal path containing `*`/`?`/`[`. Read safe.ts and safe.mjs for the real parameters and path resolution (cwd) and make the
  suggestion resolve the same way in both. If a target cannot be named faithfully, fall back to F1's generic guidance; never
  suggest a call that searches somewhere else.

## F3 (Closes by: code): the guidance matches what is refused
For several searches in one command, name a safe call for each sensitive one (the one the classifier refused, e.g. TOKEN in
config/), not the first grep. Harmless searches in the same batch may be named too, but the sensitive ones must be there.

## F4 (report checks)
- Add grep -C forms to the attack rows (the brief asked for them).
- Show the sensitive_trust fail-before log, or narrow that claim.
- State the attack as before/after row pairs as the log shows them (912 per version), not 2,736.
- Main corrects its own count (the four-cell total was 812 per cell, not 867; Main's counter misread "Ran 1 test").

## Attack (realistic-use bar)
Extend round 1's table, built by code: the reviewer's repro forms; transcript forms with `;`/`&&`/`||` and a second command; two
searches in one command (harmless + sensitive, both orders); `git -C`, `<` redirect, globs; grep -C. Per row: decision before and
after, record status, and for worker rows the named safe call checked against the target the shell would search (computed by code,
not by eye). Counts before and after. No ask→allow, no deny weakened, main rows unchanged.

## Report
Fail-before against 664ec57 for each new test; the four cells (`/usr/bin/python3` and `/opt/homebrew/bin/python3`, outside and inside
tmux as in round 1), one after another, with tests.test_session_env, tests.test_sensitive_read, tests.test_sensitive_trust,
tests.test_trust_guard, tests.test_claude_redact_hook, tests.test_redact, tests.test_pi_redaction, tests.test_pi_safe_tools,
tests.test_orchestration, tests.test_worker_env; each log starts with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`,
then `git reset -q`; per-module times. Search with safe_grep and read with Read/safe_read; never search `bin/` as a directory or
`config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*` or auth files. Test strings that name `config/`
or `.env` paths go in a script file, not shell text. Nothing sent to 127.0.0.1:1234 or any network address; no Claude or pi start.
`git add` new files. Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells.
