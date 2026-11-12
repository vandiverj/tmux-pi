# HB5b: Main's verification leaves records the repeat report counts (lane hub-main-runs)

Owner (2026-11-10): "My priorities are understanding where work waits and avoiding repeated verification." HB5a
(`hb5a-brief.md`, `hb5a-decision.md`, `tools/checks/repeats.py`) found that the large repeat (Main's cells rerunning what the
worker ran, on the same tree) is invisible because Main's runs leave no record. The decision named the next step: **record
Main's runs**, with the interpreter in the record, before any shared fingerprint or `tp-check` reuse. This step is that and
nothing more. It is **advisory**: nothing skips a run because of a record. Bar: realistic use (`docs/plans/realistic-use-bar.md`).

## Rules first (write this table in report.md before code)
1. **Where Main's records go.** Main runs `tools/agent/test-run.sh` with no `TP_TASK_DIR`. Add one way for Main to say "this is
   Main's run" (for example `--main [--lane L --step S]`) that writes the record under the session, in a place
   `tools/checks/repeats.py SESSION_TASKS_DIR` reads with role `main`. Find how test-run.sh and tp-agent resolve the session
   state dir; say which you used and what happens with no session (one clear error, no record written beside some unrelated
   path). Numbering uses the same mkdir claim as task records. The record stays `tp-test-run:v1`; check that
   `lib/gate_record.validate` and every other reader of these records accept any field you add (list the readers).
2. **Which interpreter ran.** A record gains the interpreter that ran the tests: the resolved path and version of the
   python the cmd starts (after the wrapper heads repeats.py already reads: `env`, `perl -e 'alarm N; exec @ARGV'`), or
   `null` when the cmd starts no python or it cannot be resolved. Say how it is resolved from `cmd[]` and the PATH the cmd
   sees, and its cost (time once per run). repeats.py then treats two runs as the same check only when their interpreters
   agree, whenever both records carry one; a record without the field keeps today's rule. A py3.9 cell and a py3.14 cell
   are never the same check.
3. **What the report shows.** Role `main` runs join the same groups as worker and reviewer runs. Add per lane and step the
   seconds Main spent rerunning checks a worker or reviewer had already run on the same tree. Nothing else in the report
   changes for records without the new fields.

## Attack table (one table in report.md, not one test per row)
The input space includes the following:
- **Flag:** `--main`, with lane/step and without.
- **Environment:**
  - `TP_TASK_DIR` set (a worker): the flag is refused, or ignored with a stated reason;
  - no session.
- **Concurrency:** two Main runs at the same moment.
- **cmd head:**
  - `/usr/bin/python3`;
  - `python3` from PATH;
  - `python3.14`;
  - through `env -u ...`;
  - through the perl alarm wrapper;
  - a non-python cmd (`bash x.sh`);
  - a missing interpreter.
- **Records:** with and without the interpreter field.
- **Relation:** Main's run on the same tree as a worker run, on another tree, with another interpreter.

For each row, give the expected output and the row that proves it. Show fail-before where the rule replaces today's reading: a py3.9 run and a py3.14 run of the same modules must not group on HEAD's repeats.py if the current key groups them; show what HEAD does.

## Tests and verification
Extend `tests/test_check_repeats.py` (its table) and the existing test-run.sh tests (find them with safe_grep). Do not add a new module, and add no ritual tests. Run once each, through `tools/agent/test-run.sh`:
- the touched test modules;
- `tests.test_session_env`.

Use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), a fresh short `TMUX_TMPDIR` under /tmp, and run in the foreground, one after another.

Case 2 (read-only): run the new report on this Mac's real session dir, then say what it shows. It will show nothing for Main until Main uses the flag, so say that. Quote no log text.

Out of scope, so do not build these:
- the env fingerprint;
- `tp-check`;
- any skip or reuse;
- a change to tp-agent, tp-review, gate records, lane close, `claude/main-prompt.md` or hooks. Telling Main to use the flag is the owner's call.

Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
