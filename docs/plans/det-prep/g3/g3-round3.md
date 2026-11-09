# G3 round 3 (Main, 2026-11-09)

Review review-coder-cca7c4bbae-052134 is REQUEST_CHANGES with F1 to F3. Read its result.md and report.md in this task's
reviews. Round 1's F1 to F5 are confirmed fixed; keep them fixed.

Main checkpointed round 2 as commit 9263f08 on lane/g3-smoke.

## Rule for every fix: the run only removes what it made

Each run gets a run id that cannot collide: random, plus pid, plus time. Session names, lane names and branch names include
it, so two runs started in the same second never share a name.

Record a resource as "made by this run" only after its creation has succeeded:
- the session;
- the lane record;
- the branch;
- the worktree.

Cleanup removes only recorded resources. Anything else it finds is reported as a leftover and left in place. The existing
"reported, not removed" rule for leftovers still holds.

## F1: cleanup kills a session it did not make

Today `session_made` is set before `new-session`. Fix it this way:
- Set the flag only after `new-session` succeeds.
- A definite refusal, such as `duplicate session`, means the run never owned the session. Do not kill it.
- A timeout is indeterminate. Kill the session only if it carries this run's marker, for example a session option
  `@g3_run=<run id>` set in the same tmux call that creates it, and the marker is read back before the kill.

Tests, in fake mode with the shim:
- a session of the same name already exists, and survives with its panes;
- creation times out and the marker matches, so the session is killed;
- creation times out and the marker does not match, so the session is kept and reported.

## F2: setup refusal deletes a branch it found

- Do not run `git branch -D` or `lane remove --abandon` unless this run created that branch or lane.
- Setup checks for name collisions before it creates anything.

Tests, in a disposable repository:
- A pre-existing `lane/<name>` branch with an unmerged commit is still present afterwards, at the same sha.
- A pre-existing lane record of the same name is still present afterwards, unchanged.
- The run fails with a message naming the collision.

Change the existing collision tamper test so it asserts that the colliding resource survives.

## F3: the trigger must be harmless when accepted, by construction

The trigger may not run any command that opens a file or prints a variable's value, whatever exists on disk or in the
environment. For example, use only the no-op builtin `:` with the operands, then `exit 3`. Find a form the hooks still ask
for in the redaction modes on, shadow and off.

Prove it in two ways:
- **The ask:** the existing in-process test, with the real hook entry points, still passes in all three modes with the new
  command.
- **Accepting is harmless:** a test makes the dotenv-shaped file exist with sentinel content, and sets the variable to the
  path of another sentinel file. It runs the exact command under `bash -c` in a temp directory and asserts:
  - neither sentinel appears in stdout or stderr;
  - the exit code is 3;
  - the command parses (shlex) to only the builtins `:` and `exit`.

  Create the sentinel files in Python under a temp directory. The test must not name a real `.env` path. Build the names
  from pieces, as today.

If no harmless form asks in all three modes, report `RESULT_STATUS: blocked` with what each hook did for each candidate.

## Done means

Same as g3-e2e-smoke.md:
- four cells, each file under 60 s;
- the scripts you wrote and ran are listed;
- `RESULT_STATUS` is reported.

The isolation rules from g3-resume-1.md still hold:
- `TMUX_TMPDIR` is a temp directory set before the first tmux call;
- a private `-L` socket from the constructor;
- the PATH shim audit.

Update the README to match. Never run the live mode.
