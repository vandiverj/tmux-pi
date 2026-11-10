# FG-N1 (WRAPPERS): command wrappers the detectors do not see through

Lane `fg-wrappers`, step N1. Level: `mid` coder, effort high (security detector). Base: `main`. Touches:
`lib/redact_hook.py`, `lib/sensitive_read.py`, `tests/test_safe_source_redaction.py`, `tests/test_claude_redact_hook.py`,
`tests/test_sensitive_trust.py` (only where its wrapper cases live), and a new `tests/test_wrapper_lists.py` if cleaner.

## Why
F-GREP round-5 review (review-coder-4d766446d8-074323, run-log 2026-11-08) note N1: wrappers missing from
`lib/redact_hook.py` `WRAPPERS` (setsid, busybox, ssh, `script -c`, flock) are not detected, so `setsid rg KEY .` is not seen
as a search and its output is not redacted. The sensitive-read detector has the same kind of list:
`lib/sensitive_read.py` `_WRAPPERS` (used by `_program`), which also lacks `setsid`, `time`, `xargs`, `chrt`, `taskset`,
`flock`, `unbuffer`, `script`, `busybox` and others, so `setsid cat .env` may not be judged a read. `lib/kill_guard.py`
`WRAPPERS` is a third list (has setsid, lacks others): report only, do not change it here.

## Change
1. Reproduce first, on main, under `/usr/bin/python3`: for each of `setsid`, `busybox`, `flock`, `script -c`, `ssh`
   (and each other wrapper you add) show (a) `redact_hook.bash_searches("<wrapper form> rg TOKEN .")` and (b) the
   sensitive-read judgement for `<wrapper form> cat .env` (call the module's own entry point, never open a real `.env`;
   use a temp dir and a fake name the detector treats as sensitive). Save `before.log`. Do not read any real `.env*`.
2. Add the missing wrappers to both lists, with their real forms:
   - `setsid`, `unbuffer`, `chrt`, `taskset`, `sg`, `runuser`, `chroot`, `nsenter`, `unshare`, `firejail`,
     `sandbox-exec`, `arch` (macOS `arch -arm64 cmd`), `gtimeout`, `time`, `xargs` where missing;
   - `busybox APPLET args` (the applet is the command);
   - `flock [opts] FILE cmd ...` and `flock [opts] FILE -c 'cmd'`;
   - `script [opts] -c 'cmd' [file]` (util-linux) and macOS `script [-q] file cmd ...`;
   - `su [-] user -c 'cmd'`, `ssh host cmd ...` / `ssh host 'cmd'` (the remote command is a command line: treat it
     as one; uncertainty is a search/read, fail closed).
   A wrapper whose command lives in a `-c` string is read with the existing whole-command-line path (bash_searches on the
   string); the sensitive-read side must do the same or fail closed. Say for each wrapper which form you handle and why.
3. Keep the lists from drifting: either one shared table both modules import, or a test that asserts the two sets
   (excluding documented differences) are equal. Pick one and say why.
4. Tests (import `session_env` first): every wrapper form above detected in both modules; a wrapper with no command
   after it is not a false positive where the module's design says so; the 64-operand limit and depth limit still fail
   closed. They must fail on main before the change (show it).
5. Run test_safe_source_redaction, test_claude_redact_hook, test_redact_llm, test_sensitive_trust, test_sensitive_read,
   test_trust_guard, test_tp_claude_suffix and any new test, both modes (session keys unset; dummy `TMUX`/`TP_*`),
   under `/usr/bin/python3` and `/opt/homebrew/bin/python3`. Nothing outside `baseline-failing.txt`.

Out of scope: kill_guard (report its gaps), F-PATHNAMES, F-BASHOUT, the N2 over-redaction note.

## Done means
1. Every listed wrapper form is seen through by both detectors, shown by tests that fail before.
2. The two lists cannot silently drift (shared table or equality test).
3. No new failure outside the baseline, both interpreters, both modes.
