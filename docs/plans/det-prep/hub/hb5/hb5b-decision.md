# HB5b: merged; Main's use of `--main` is the owner's call

Merged b2c9f3a (lane hub-main-runs, 3 rounds, review-coder-5243a36008-165341 APPROVED_WITH_NOTES).

## What it adds
- `tools/agent/test-run.sh WORKTREE --main [--lane L] [--step S] -- CMD...` writes Main's own test run to
  SESSION/main-runs/<n>.json (role main). A worker (TP_TASK_DIR set) is refused; no session gives one error and no record.
- Every record carries the interpreter that ran (`{path, version}` or null, tools/agent/interpreter.py). repeats.py treats
  two runs as the same check only when their interpreters agree, so a py3.9 cell and a py3.14 cell never group.
- repeats.py reports, per lane and step, the seconds Main spent rerunning a check a worker or reviewer had already run on
  the same tree. A worker or reviewer run counts only when its end time is strictly earlier than Main's; ties count 0.

## Rounds
| Round | Commit | Review | Result |
|---|---|---|---|
| 1 | 0fd8e0e | REQUEST_CHANGES | Main reruns undercounted; relative PATH resolved against the caller's cwd; no real overlap in the concurrency test |
| 2 | ee7cbef | REQUEST_CHANGES | equal end times broke by task name (Main charged a rerun on a tie) |
| 3 (rule-first) | cfa764f | APPROVED_WITH_NOTES | strict end-time order; 64/64 sweep cases |

## Owner decision needed
Today the report shows nothing for Main, because Main's cells do not pass `--main`. Main's verify scripts call
tools/agent/test-run.sh, and adding `--main --lane L --step S` there needs no prompt change. Telling Main to always do it
is a line in claude/main-prompt.md, which needs the owner's OK.

- **A (Main recommends).** Main's verify scripts pass `--main` from now on. Add one line to claude/main-prompt.md: "Run your
  own verification through tools/agent/test-run.sh --main --lane L --step S." After a few lanes, the repeat report shows
  how much of Main's cell time repeats the worker's runs. That is the evidence for or against reuse (the next HB5 step).
- **B.** Only Main's verify scripts pass it; no prompt change. Records exist only while this Main session's scripts are in use.
- **C.** Neither; the flag stays unused.
