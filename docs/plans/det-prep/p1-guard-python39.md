# P1: the trust guard crashes on any glob under the system Python (3.9)

## Defect (found live, 2026-11-07)

`lib/trust_guard.py:532` calls `glob.iglob(pattern, include_hidden=True)`. `include_hidden` exists only from Python 3.11.
The hooks run `#!/usr/bin/env python3`, which here is 3.9.6. Any tool call whose words reach `_glob` (a shell word with
`*`, `?` or `[`, a Glob/find pattern) raises `TypeError`; `_decide` turns every exception into `ANALYSIS_FAILED`, so the
call is **denied** with "the tool call could not be analysed. Try the call again". Fail-closed, so nothing leaks, but:

- every worker command with a glob word is refused (round 3 of T1, `coder-eead542ff6`, was denied nine times on
  `tp-agent request ... --what "... tests/test_*.py ..."` and could not ask Main for scope);
- the remedy text tells the agent to retry a call that will always fail;
- about 60 lines of `docs/plans/det-prep/baseline-failing.txt` (`test_trust_guard`, maybe `test_sensitive_trust` and
  `test_trust_record`) are this defect, accepted as "already failing", so the suite has been hiding it.

Repro: from any directory outside the store,
`echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls tests/test_*.py"},"cwd":"'$PWD'"}' | TP_TASK_ID=x TP_MODE=worker /usr/bin/python3 bin/tp-trust-guard --claude-hook`
prints a deny.

## What to build

1. **One glob implementation that does not depend on the interpreter.** `_glob` matches hidden names as
   `include_hidden=True` does on 3.11, with the same budget and deadline checks per entry visited (`self.entries`,
   `self.deadline`, `_ran_out`, `_OutOfTime`), on 3.9 and on 3.11+. Do not branch on `sys.version_info`: the tests must
   exercise the code the hook runs. Keep `**` handling as it is today (`_globstar` does the walk; `_glob` sees `**` as
   `*`, as `iglob` without `recursive` does). Fail-closed stays: a walk that cannot finish still denies or asks exactly
   as today.
2. **Sweep for the same class.** Every module the hooks and gates import (`bin/tp-trust-guard`, the redaction hook, the
   safe MCP server, `lib/sensitive_read.py`, `lib/trust_guard.py`, `lib/redact_*`, and what they import): list every
   call to an API or syntax newer than 3.9 (keyword arguments added after 3.9, `match`, `X | Y` types evaluated at
   run time, `str.removeprefix` is 3.9 so fine, etc.). Report each as a sweep row with file:line and fixed / not
   applicable.
3. **Make an analysis failure diagnosable.** `ANALYSIS_FAILED` keeps denying, but its remedy must not say "try again"
   when retrying cannot help, and the exception class and the guard's file:line must reach the owner (stderr of the
   hook, or the existing log if there is one). Never put the command text or any path from it in that line.
4. **Tests (in `tests/test_trust_guard.py`).**
   - A parity table: patterns (plain `*`, `?`, `[ab]`, `[!a]`, hidden file `.x`, hidden dir `.d/*`, nested `a/*/b`,
     no wildcard, a missing directory, a pattern ending in `/`, a symlink to a directory, `..` in the pattern) against a
     temp tree, expected list written out in the test (not computed by `glob` at run time).
   - A budget test: a tree larger than the entry budget still triggers `_ran_out` under the new code.
   - A subprocess test that runs `bin/tp-trust-guard --claude-hook` under `/usr/bin/python3` (skip with a reason if
     absent) on `ls tests/test_*.py` from a temp project, and asserts allow; and one on a glob that can reach the store
     and asserts deny. This must fail before the fix.
5. **Baseline.** Run `test_trust_guard`, `test_sensitive_trust` and `test_trust_record` under the system `python3`
   before and after. Report per baseline line (by line number of `baseline-failing.txt`) whether it now passes. Do not
   edit the baseline file; Main removes the lines at merge.

## Done means

- The repro above allows `ls tests/test_*.py` and still denies a glob that reaches the trust store, under 3.9.
- All three modules run per module, in both modes (outside a tmux session and with dummy `TMUX`/`TP_*` set), with the
  table of command, count, seconds, outcome in `result.md`; every remaining failure is matched to a baseline line.
- The sweep table from step 2.
- No change outside `lib/trust_guard.py`, `tests/test_trust_guard.py` and any file the sweep fixes (name each).
