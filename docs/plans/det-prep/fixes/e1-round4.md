# E1 round 4 (route: design) — parse git's global options; stop matching forms with a regex

Rounds 1–3 all failed in family probe-specificity. Each round extended `GIT_FETCH` (`lib/routines.py:36`) for the form the reviewer cited, and the reviewer then found the next one: `--prune`, then `-C x`, then a quoted ref, then `-c k=v`. A regex can't know which git options take a value. This round replaces that approach with a rule.

## Change (lib/routines.py only, plus tests)
1. Add `git_subcommands(cmd)` just above `keys_for`. It returns, for each simple command in `cmd`, the git subcommand and its span, or nothing for a command that isn't git.
   - Split `cmd` into simple commands on `;`, `&&`, `||`, `|` and newline, outside quotes. Use `shlex.shlex(cmd, posix=True, punctuation_chars=True)` or a small scanner. On `ValueError` (unterminated quote), fall back to whitespace split; never raise.
   - Skip leading `VAR=value` assignments. The command word must be `git`, or a path ending in `/git`.
   - Walk git's global options as listed in `git --help`:
     - `-C` and `-c` consume the next token.
     - `--git-dir`, `--work-tree`, `--namespace`, `--super-prefix`, `--config-env` and `--exec-path` take a value either as `=value` or as the next token. `--exec-path` with no value is a flag.
     - Every other token starting with `-` is a flag. That includes `-p`, `-P`, `--paginate`, `--no-pager`, `--bare`, `--no-replace-objects`, `--literal-pathspecs`, `--glob-pathspecs`, `--noglob-pathspecs`, `--icase-pathspecs`, `--no-optional-locks`, `--no-advice` and `-v`/`--version`/`-h`/`--help`.
   - The first token that is not an option is the subcommand.
2. In `keys_for`:
   - Any simple command whose git subcommand is `fetch` yields `FETCH_PROBE`, and its text is removed from what the other probes see.
   - Delete `GIT_FETCH` and the fetch special-casing around `lib/routines.py:55–60`.
   - Lane-base keeps matching `merge-base` and refs in every non-fetch simple command.
3. Keep every behaviour that rounds 1–3 fixed: `which prettier`, token columns for dropped probes, and probe ordering.

## Tests (tests/test_routines.py): one table-driven test
- Positive rows. Each must give first probe `git fetch` and have lane-base absent:
  - `git fetch`
  - `git fetch origin/develop`
  - `git -C x fetch o/develop`
  - `git -C 'a b' fetch "origin/develop"`
  - `git -c protocol.version=2 fetch origin/develop`
  - `git -C x -c a=b fetch --prune origin/DMP-1`
  - `git --no-pager fetch o/develop`
  - `git -p fetch o/develop`
  - `git --git-dir=.git fetch o/develop`
  - `git --git-dir .git --work-tree . fetch o/develop`
  - `git --namespace=n fetch o/develop`
  - `git --bare fetch o/develop`
  - `git --exec-path=/x fetch o/develop`
  - `git --config-env=a.b=ENV fetch o/develop`
  - `FOO=1 git fetch o/develop`
  - `/usr/bin/git fetch o/develop`
  - `cd x && git fetch origin/develop`
  - `git fetch o/develop | tail -3`
- Negative and mixed rows:
  - `git merge-base HEAD origin/develop` → lane-base and not fetch.
  - `git log origin/develop` → not fetch.
  - `git -c a=b merge-base HEAD origin/develop` → lane-base.
  - `git fetch origin && git merge-base HEAD origin/develop` → both probes.
  - `echo "git fetch origin/develop"` → not fetch.
  - `git fetch 'unterminated` → no exception.
- One parity test. It holds a literal list of the value-taking global options from `git --help` (`-C`, `-c`, `--git-dir`, `--work-tree`, `--namespace`, `--super-prefix`, `--config-env`, `--exec-path`). For each one it builds `git <opt> <val> fetch o/develop`, plus the `=` form where git allows it, and asserts fetch. This proves the rule, not a sample.
- Show the table fails on the old code: run it with `lib/routines.py` from HEAD and quote the failure count.

## Report
- For each module: the command, the test count, the seconds and the log path (output redirected to `$TP_TASK_DIR/tests.log`).
- A sweep table of every site that inspects the command text for git.
- Do not commit.
