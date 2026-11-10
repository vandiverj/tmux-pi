# K1: plain `git grep` over a credential-named folder doesn't prompt when git tracks no credential file there

Lane `sensitive-grep`, step k1.
- Level: high. This is security classifier code; a cross-provider review is required.
- Touches: `lib/sensitive_read.py`, `tests/test_sensitive_read.py`, `tests/test_sensitive_trust.py`, `docs/sensitive-read-redaction-design.md` (one paragraph only).

## Why
Measured on 2026-11-07: 8 of 17 unattended reviews stopped on "Authorize sensitive read?". Every one was a `git grep … agent/`, `config/` or `tools/agent` inside a lane worktree, and each stop blocks unattended work until a human answers.
- `lib/sensitive_read.py:61` (CREDENTIAL_DIRS) treats a folder named `agent`, `config` or `claude` as a bulk root that may hold credentials.
- `_credential_dir` (`:686`) only lifts that when the owner's trust walk clears it.
- A plain `git grep` reads only files in the index. This repo's credential files (`agent/auth.json`, `agent/sessions/`, `config/local.json`) are gitignored and are not tracked in any worktree. So none of those searches could reach a credential.

## Change
1. Define a *plain* git grep: one with no `--untracked`, `--no-index`, `--no-exclude-standard`, `--recurse-submodules` or `-O`/`--open-files-in-pager`, and no revision or tree operand. The existing `_REVISION`/`_not_a_path` check already flags those; keep it.
2. For a plain git grep, the credential-folder reason (`_CREDENTIAL_DIR`) and the repository-root reason are lifted for a root when the tracked files under it contain no credential-named path.
   - Find the tracked files with `git -C <repo> ls-files -z -- <root>` (it reads the index only and has no side effects).
   - Credential-named means the same `_named`/`_FILE_SAMPLES` rules the classifier already applies to single paths.
   - If `ls-files` fails, times out (keep the existing subprocess timeouts), or the tracked list is too large to check within the existing caps, the reason stays: fail closed.
   - A tracked credential-named file (for example a committed `config/local.json`) keeps the prompt.
3. Every other path keeps its current verdict: `grep -r`, `rg`, the Grep tool, a non-plain git grep, a root outside the repository, and a revision search.
   - The trust-store guard (`lib/trust_guard.py`) is not touched and still runs first.
4. Reuse the module's existing walk, lift and caching machinery (`_walk_lift`, `_SHELL`) rather than a parallel path, if it fits. Say in the report which you used and why.

## Tests
- A table test in `tests/test_sensitive_read.py`, built on a temp git repo that has tracked `agent/x.md` and `config/a.json`, plus gitignored `agent/auth.json` and `config/local.json`. Rows:
  - `git grep -n foo -- agent config` → not sensitive;
  - `git grep foo agent/` → not sensitive;
  - `git grep --untracked foo agent` → sensitive;
  - `git grep --no-index foo agent` → sensitive;
  - `git grep foo HEAD -- agent` → sensitive;
  - `git grep foo` at the repo root → not sensitive;
  - `grep -rn foo agent` → sensitive (unchanged);
  - `rg foo config` → sensitive (unchanged);
  - the same repo with `config/local.json` force-added → `git grep foo config` sensitive;
  - a broken `.git` or a failing ls-files → sensitive;
  - piped and compound forms, as `git grep foo agent | head` and `cd sub && git grep foo ../agent`.
- Show that the not-sensitive rows are sensitive on the current code, using a copy of the old file, and quote the counts.
- Run `tests.test_sensitive_read`, `tests.test_sensitive_trust` and `tests.test_claude_redact_hook`, one module at a time. If you change a JS-visible verdict, also run the node runtime tests that already cover the classifier (`tests/sensitive_runtime.mjs`, as the repo runs it).

## Done means
1. The not-sensitive rows above don't prompt, and every unchanged row still does.
2. Each failure mode stays sensitive: a tracked credential file, a failing ls-files, or a non-plain git grep.
3. The design doc paragraph on git grep says what is lifted and why.
4. Report:
   - for each module, the command, the test count, the seconds and the log path (output redirected to `$TP_TASK_DIR/tests.log`);
   - a sweep table of every place git grep is judged.
5. Do not commit.
