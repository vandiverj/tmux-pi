# G3 round 2 (Main, 2026-11-09)

Review review-coder-1469cd1f98-043938 is REQUEST_CHANGES with F1 to F5 (its result.md and report.md, in this task's reviews). Main checkpointed
round 1 as commit 0e88cb9 on lane/g3-smoke. Fix all five:

- **F1:** the prompt trigger must actually ask in the mode workers run in. Read how a worker gets `TP_REDACTION` (tp-spawn,
  `worker_env`, config/router.json as one named file) and which hooks ask in that mode: lib/redact_hook.py, lib/sensitive_read.py,
  bin/tp-sensitive-read, bin/tp-trust-guard, and the Claude hook config the worker loads. Pick a trigger whose ask is decided by
  code and not by the model's judgement, and that can never read a real secret. For example: a shell command the guard always
  asks for, aimed at a path that does not exist. Prove the ask with an in-process test that calls the real hook entry point
  with the exact event the worker's tool call would produce, in the worker's mode. That test must not run a shell command
  whose text names a `.env*` path: build the string in Python. If no trigger asks in every mode workers use, report it with
  `RESULT_STATUS: blocked` and list what you found.
- **F2:** do not run a repository-wide `git worktree prune`. Remove only this run's worktree, by path.
- **F3:** `--inject-failure` and `--tamper` are allowed only with `--fake`. Refuse them otherwise, and test the refusal.
- **F4:** every subprocess call has a timeout, including the leftover probes and the cleanup. A timeout is recorded as a
  cleanup error and does not stop the remaining cleanup or the JSON summary. Test it with a tmux shim that hangs.
- **F5:**
  - a tampered-record case for every record check the README lists;
  - a real leftover case: something left on purpose that the leftover check must name;
  - the module audit compares each `kill-server` socket and TMUX_TMPDIR with that run's own, not by prefix.
- Update the README to match.

Same Done means as g3-e2e-smoke.md: four cells, each under 60 s; scripts listed; `RESULT_STATUS`. The isolation rules from
g3-resume-1.md still hold. Never run the live mode.
