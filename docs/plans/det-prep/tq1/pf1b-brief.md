# PF1b: remove the quadratic whitespace test and the repeated git call (Main, 2026-11-11)

PF1a (coder-9155b7e965; `report.md` and `evidence/` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/coder-9155b7e965/`)
measured: `bin/tp-spawn:198` `[[ -n "${task//[[:space:]]/}" ]]` takes 3.17 s on a 3988-character review task (quadratic in bash
3.2 with a UTF-8 locale); every review and long dispatch pays it. Main's decision (owner delegated): PF1a proposals 1, 4, 5 now;
2 (batched blob diff, safety-sensitive), 3 and 6 are not in this lane. Lane perf-spawn, base main. Kind: mechanical.

## Changes
1. **Every** `${var//[[:space:]]/}` (or similar whole-string substitution used only as an emptiness/whitespace test) in `bin/`
   and `tools/` shell scripts: replace with a regex test (`[[ "$var" =~ [^[:space:]] ]]`) where the substitution's result is only
   tested; leave any whose result is used. List every occurrence found (safe_grep over the repo's shell files, not only the two
   PF1a named) with its disposition `fixed` / `result used, kept`.
2. PF1a proposal 4: in the dispatch path, compute `git rev-parse --git-common-dir` once per command invocation (no cache across
   invocations, no global state that outlives the process).
3. PF1a proposal 5: precompile the copied lib in the test fixture that copies it (one `compileall` call, quiet) if it saves time
   measured on test_tp_review; if it does not, drop it and say so.

## Attack (oracle first, from bash semantics)
For each changed test: inputs empty, space, tab, newline, CR, mixed whitespace, NBSP (U+00A0), other Unicode spaces (U+2003,
U+3000), a 4000-character and a 100 000-character task, a task that is only whitespace plus a NUL-free control char, under
`LANG=en_US.UTF-8` and `LC_ALL=C`, with bash 3.2 (/bin/bash). Expected: new form gives the old form's answer on every input
(write the old form's answer from running it, not assumed); timings for the 4000 and 100 000 cases before and after.

## Tests (tests earn their place)
Extend the existing tp-spawn task-argument test (find it) with the empty and whitespace-only rows if no row covers them; the
attack table stays a report artifact. Fail-before is not expected (behaviour-preserving); show the timing before/after.

## Verification (the worker tests once)
Affected modules once (tp-spawn, tp-review, trial_shadow, lane writer tests, and tests.test_session_env), `/usr/bin/python3`
outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), fresh short
`TMUX_TMPDIR` under /tmp, `perl -e 'alarm 900; exec @ARGV'`, through `tools/agent/test-run.sh`, log starting with `date -u`,
`TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Runs under 10 minutes, one after another, foreground.
Main runs the cells and discovery. No Claude or pi start; nothing to 127.0.0.1:1234 or any network; never the default tmux
server. Do not edit config/. Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth files or
config/local.json. **Guard refusals:** if a guard hook refuses a shell or inline-script edit, make the same edit with Edit/Write
and list it; any other refusal: stop and report. Approve no prompt. Commit nothing. No background shells. `RESULT_STATUS:
complete` as the last line.
