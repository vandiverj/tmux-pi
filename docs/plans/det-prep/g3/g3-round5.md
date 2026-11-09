# G3 round 5 (Main, 2026-11-09)

Review review-coder-4f7e7bc027-063549 is REQUEST_CHANGES with F1 and F2 (code) and F3 (reporting). Read its result.md
and report.md in this task's reviews. Main checkpointed round 4 as commit 272c490.

Rounds 1 to 4 each found a new site of a rule that was already known. This round fixes each site, then sweeps the whole
script for every other site of the same two rules. Give one sweep table per rule, covering every site: fixed, already
right, or not an instance, each with evidence.

## Rule A (family bounded-subprocess): no unbounded work in the smoke process
Every external process the script starts goes through `bounded()`, with the run's scrubbed env. That includes processes
started inside imported library code.

- **F1.** `lanes.load_repo_config` and `lanes.lane_branch` reach `subprocess.run(["git", "check-ref-format", ...])` with no
  timeout. Compute the names in a child process instead: a small `python3 -c` or helper module that imports lanes and
  prints JSON, run through `bounded()` with the run's env and timeout. Do not change lib/.
- **Sweep.**
  - List every call from the script into lib/ or other imported tool code.
  - For each, trace whether it can start a process, open a socket or block on I/O.
  - Any that can must run in a bounded child, or the table must say why it cannot block.
- **Test.** A git shim on PATH that hangs, with a small `--subprocess-timeout`. The run must still finish with a summary
  that names the config-load timeout, and must leave nothing behind. Replace the source-string counting test, or add to it,
  with a test that catches imported calls. For example: monkeypatch `subprocess.Popen` and `subprocess.run` in-process and
  fail on any call that does not come from `bounded()`.

## Rule B (family cleanup-ownership): check exact computed resources, never name fragments
Every collision check and every leftover check compares the exact computed resource. No substring or basename match.

- **F2.** Check the exact worktree path from `lanes.lane_worktree`: registered in `git worktree list`, or an existing
  directory or file at that path, before anything is made. The leftover probe recognizes that exact path. Keep the broad
  run-name detection as an extra, reported only. Never remove a pre-existing collision.
- **Sweep.** Check every name or path test in setup, claim, leftovers and cleanup against this rule.
- **Tests.** A configured `worktree_pattern` of `{repo}-{name}/checkout`, in four cases:
  - registered worktree at the path;
  - plain directory at the path;
  - file at the path;
  - the parent directory exists but the path does not.

  Each case is refused at setup or proceeds, as appropriate, and anything pre-existing survives. Leftover reporting names
  the exact path.

## F3 reporting
- Time every test over 5 s alone with `-k`, in every module. That includes every test that pays for a shared batch when
  run alone, such as `test_failure_at_each_step`.
- Correct the tamper and case counts from `len(g3.TAMPERS)`.

## Done means
Same as before:
- four cells, each file under 60 s;
- the scripts you wrote and ran are listed;
- `RESULT_STATUS` is reported.

The isolation rules from g3-resume-1.md still hold. Keep every earlier fix. Never run the live mode. Do not change lib/ or
bin/; if a rule cannot be met without a product change, report `RESULT_STATUS: blocked` and give the reason.
