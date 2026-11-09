# G3: the end-to-end smoke script

`tools/smoke/g3_e2e.py` drives the real `bin/` tools as subprocesses through the eight G3 steps
(dispatch, a declined permission prompt with a redirect, retry, cross-provider review, `lane close`, merge, `collect`, `lane remove`),
then checks that nothing of the run is left. Python 3.9, standard library only.
Tests: `tests/test_g3_e2e.py` (flow, injected failures, hook trigger, guards) and `tests/test_g3_e2e_tamper.py` (a broken record for every check);
both use `tests/g3_e2e_support.py` (the tmux shim and the audit).

## The live command (Main runs it; the lane never did)

From a tmux-pi checkout that has this script, inside the tmux server that Main uses (the default `TMUX_TMPDIR` is kept):

    python3 tools/smoke/g3_e2e.py --repo ~/Documents/Projects/battle-soccer --out .tmux-pi/g3/live-$(date -u +%Y%m%d%H%M%S).json

Exit 0: every step and the cleanup passed. Exit 1: stderr says `FAILED at step '<name>': <what>`, and `cleanup: ...` / `left behind: ...` lines.
`--out` gets the JSON summary (run, run_id, mode, ok, failed_step, error, `made` (what the run recorded as its own), socket and tmux_tmpdir (fake only), per-step name / rc / seconds / asserted list / commands,
cleanup report (errors, leftovers, kept), task ids). Useful flags: `--keep` (keep the temp state dir), `--coder-model` (default `claude-haiku-5-5`), `--prompt-wait 240`,
`--settle-wait 600`, `--review-wait 600`, `--subprocess-timeout 30`.

What it makes, all named `g3-<run id>`: a tmux session, a lane (worktree and branch `lane/g3-<run id>` of battle-soccer), and one scratch
integration branch `lane/integrate` (created at battle-soccer's current HEAD). battle-soccer's own branch never moves (asserted). The state dir
(task records) is a temp dir of the run, not Main's.

**The run only removes what it made.** The run id is `MMDDHHMM-<pid in base 36>-<6 random hex digits>` (time, process id and 24 random bits, so two runs
started in the same second never share a name; `g3-` plus the id is at most 24 characters, the limit of a lane name). Setup checks every name before it
makes anything, and refuses (step `setup`, the error names the collision) if any of these already exists: the branch `lane/integrate` or `lane/g3-<run id>`,
a lane record of that name, a worktree of that name, a tmux session of that name. The cleanup then removes only resources recorded as made by this run, and a
resource is recorded only after its creation succeeded:
- the scratch branch: recorded after `git branch` succeeded; the lane record, lane branch and worktree: made by `tp-agent dispatch`, recorded after the call
  (also when it failed part way) because setup had proved the names free and they carry the run id;
- the tmux session: one tmux call creates it and marks it (`new-session ... \; set-option -t =<name>: @g3_run <run id>`), and it is recorded after that call
  succeeded. A definite refusal (`duplicate session`) records nothing and kills nothing. A timeout is indeterminate: the cleanup reads the marker back, and
  kills the session only if it is this run's id; any other marker is kept and reported (`cleanup.kept`);
- a recorded task pane is killed only if tmux lists it in this run's session.

Whatever the run finds and did not make stays in place, and the leftover list and `cleanup.kept` name it. Nothing runs `git branch -D` or `lane remove --abandon`
for a branch or lane the run did not create.

`--inject-failure` and `--tamper` are test hooks and exist only with `--fake`: without it the script exits 2 before it makes anything
(a live run would start real workers before the hook failed it). `--tamper` also refuses `--repo`: a tamper edits the repository (`not-git` deletes `.git`), so it runs only on the throwaway repository a fake run makes.

## Cost (live)

One coder task that is declined and redirected (it makes no change), one retry coder task (adds one file), one review. All small:
claude-haiku-5-5 at low thinking for both coder tasks, and the reviewer tp-route picks (it must be a different provider). No other model call.

## The permission prompt: mechanism, and how deterministic it is

The first coder task tells the worker to run exactly one Bash command first (`PROMPT_COMMAND` in the script):

    : ./<a dotenv-shaped name> '$<a secret-shaped variable name>'; exit 3

(both names are built from pieces in the script; they are `.env.g3-smoke-absent` and `G3_SMOKE_API_TOKEN`). It is harmless if the owner accepts it, by construction, whatever
exists on disk or in the environment: it runs only the no-op builtin `:` and `exit 3`. `:` opens no file and prints nothing; its two operands are only text for the hooks to
match (the variable is in single quotes, so the shell does not even expand it). Accepting it does nothing and exits 3. Other forms were tried and rejected: `cat ./<name> "$VAR"`
(opens a file, and prints a variable if one is set), and forms with only one of the two operands (they ask in some modes only).

Which hook asks is decided by the redaction mode the worker runs in (`TP_REDACTION` env, else `config/local.json` `redaction.mode`, else `shadow`;
`tp-spawn` does not set it, so the worker inherits Main's mode; Main's is `on`). All three modes ask, decided by code:
- `on`: `bin/tp-redact --claude-hook` (`lib/redact_hook.py` `pre_decision`): a secret-variable reference (matched as text, quotes included) in a command it cannot wrap (the `exit`) is an `ask`;
  `handle` writes `permission-request.json` (`status: pending`, with `reason`, `target`, `call` = the tool_use_id) in `TP_TASK_DIR`. `bin/tp-sensitive-read` defers.
- `shadow` and `off`: `bin/tp-sensitive-read --claude-hook --trusted` (`lib/sensitive_read.py` `classify`): the dotenv-shaped name inline in a command is an `ask`; it writes the same
  request, `status: pending`. `lib/redact_hook.py` does not gate in these modes.
- `bin/tp-trust-guard` lets the command through in every mode.

`tp-agent inspect` then reports `status: needs_input` with a `permission` block (`lib/tp_detail.py`). The script does what Main does: sends `Escape` to the task's pane
(declines the dialog; the `PermissionDenied` event settles the request to `denied`), waits for it to leave `pending`, and sends the redirect with `tp-agent message`
("do not run that command, make no change, reply RESULT_STATUS: incomplete").

Proof (no model, no pane): `HookTriggerTest` in `tests/test_g3_e2e.py` builds the exact `PreToolUse` Bash event (and the `PermissionDenied` one) and calls the real entry points in
this process, in each mode: `redact_hook.handle` and `bin/tp-sensitive-read`'s `main()` (with the arguments `hooks/sensitive-read.sh` passes) with `TP_REDACTION` set to `on`, `shadow`, `off`
in turn. In each mode exactly the expected hook answers `ask`, the other defers, the request is `pending` with a reason and target, then `denied` after `PermissionDenied`, and the absent path stays absent.
It also checks that `hooks.json` loads both hooks for every tool, and that `tp-trust-guard` lets the event through. The test builds its strings from the script's constants; no shell command names a `.env*` path.

Two more tests show the command is harmless when accepted (`HookTriggerTest`): `test_the_trigger_is_only_noop_builtins_with_operands` parses it with shlex and requires exactly the
commands `:` (with the two operands) and `exit 3` joined by `;`, with no other operator, substitution or redirection; `test_accepting_the_trigger_is_harmless` creates, in Python under a temp directory,
the dotenv-shaped file with sentinel content and another sentinel file whose path is the value of the secret-shaped variable, runs the exact command under `bash -c` in that directory, and requires
exit code 3, empty stdout and stderr, and neither sentinel (nor the path) anywhere in the output.

What is still not 100% deterministic (live only): the worker model must follow the instruction and make that one Bash call. The script never hangs on this: it waits at most `--prompt-wait` seconds for
`needs_input` + pending, and fails at step `prompt` with "no permission prompt ... within N s (last seen: ...)"; it fails at once if the task is already settled, failed or stopped
(a worker that finished without asking will not ask). If the live run shows the model skips the call, raise `--prompt-wait` or rerun.

## What each step asserts (a failed assertion fails the step and names itself)

Each assertion below has a tampered-record case in `tests/test_g3_e2e_tamper.py` (`TAMPER_EXPECT`: tamper name -> step and the check it must name); a cross-check there fails if a check
the passing flow makes has no case.

- setup: the repo is a git checkout (`not-git`) with no uncommitted tracked change (`dirty-repo`); its own branch is not `lane/integrate` (`on-scratch-branch`); `lane/integrate` (`integration-exists`),
  `lane/<name>` (`lane-branch-exists`, with an unmerged commit on it), a lane record of the name (`lane-record-exists`), a worktree of the name (`worktree-exists`) and a tmux session of the name
  (`session-exists`, with a pane) do not exist yet; the tmux session starts. Each collision tamper is also run with `--keep` and the test asserts the colliding resource survived unchanged:
  the branch at the same sha (holding a commit no other branch has), the lane record with the same content, the worktree still registered, the session's pane still listed and no `kill-session` or `kill-pane` recorded by the shim.
- dispatch: a `coder-` task id (`dispatch-id`); started, not queued (`dispatch-queued`); `meta.json` names the lane and step `s1` (`dispatch-meta`); a pane is recorded (`dispatch-pane`);
  the lane record is `open`, branch `lane/<name>`, based on the scratch integration branch (`dispatch-lane`); the lane worktree exists (`dispatch-worktree`).
- prompt: task state `needs_input` with a pending permission request (`no-prompt`); `permission-request.json` is `pending` and carries a reason (`prompt-no-reason`).
- decline: after `Escape` the request leaves `pending` (`still-pending`) and is not `authorized` (`authorized`); the redirect is recorded in `messages.jsonl` (`no-redirect-record`);
  the task settles (`first-never-settles`) with `result_status: incomplete` (`first-complete`); the lane worktree is still unchanged (`first-changed`).
- retry: the first task is stopped (`first-not-stopped`; a lane allows one writer); `retry` starts a new task (`retry-same-id`) that keeps the lane and step (`retry-meta`);
  it settles (`task-state`, a failed exit) with `result_status: complete` (`retry-incomplete`); the worktree holds exactly the one new file (`retry-extra-file`),
  `G3_SMOKE.md`, whose whole content is the requested line (`retry-wrong-line`).
- review: `tp-review` prints a reviewer task id (`reviewer-id`); `review-of` names the reviewed task (`review-of`); the reviewer's provider differs from the author's (`same-provider`) and the plan's
  independence is `different_provider` (`independence`); `reviewed-state.json` names the reviewed tree (`reviewed-tree`); the reviewer settles (`reviewer-never-settles`) with an approving verdict
  (`APPROVED` / `APPROVED_WITH_NOTES`, `review-verdict`) and a complete review (Coverage section, `review-incomplete`).
- close: the file is committed on the lane branch and the tree is clean (`close-dirty`); after `tp-agent lane close` the lane record lists step `s1` as closed (`lane-record`).
- merge: a merge commit on `lane/integrate` (made with `commit-tree`, no checkout) with parents (previous tip, lane tip) (`merge-parents`); the branch changed exactly one file, added (`merge-extra-file`);
  that file holds exactly the requested line (`scratch-commit`); the repository's own branch did not move (`main-moved`).
- collect: `collect` of the retry and of the reviewer returns their records (`collect-id`), the tasks are `completed` (`collect-state`) and have `reaped.json` (`collect-reaped`); the panes of all three tasks are gone (`pane-alive`).
- remove: `lane remove` says removed, not abandoned, integrated in `lane/integrate` (`remove-abandoned`); the lane worktree directory is gone (`worktree-back`); the lane record is archived (`lane-back`).
- leftovers: no worktree, branch, lane record or pane of the run is left (`leftover-worktree`, `leftover-branch`, `leftover-lane-record`, `leftover-pane`: each leaves that thing on purpose and the check must name it);
  after killing the run's session, no session and no pane of it remains (`session-kept`). A probe that cannot finish is an error, not a pass.

A `finally` block always runs the cleanup (kill the run's recorded panes and, if it made it, its own session by name; `lane remove --abandon`, `git worktree remove --force <this run's worktree path>`,
delete `lane/<name>` and `lane/integrate`, each only if recorded as made by this run; remove the temp dir), then re-checks the leftovers. There is no repository-wide `git worktree prune`: other worktrees of the repository,
prunable or not, are never touched (`PruneTest` runs against a repo that has an unrelated prunable worktree). Every wait is bounded, with an error that names what was waited for.

Every subprocess has a timeout: steps use their own (git 60 s, tools 120 s, `tp-review` 180 s), every tmux call, leftover probe and cleanup command uses `--subprocess-timeout` (cleanup git and lane commands: twice that).
A command that times out is killed (a grandchild that holds its pipes is given up after 2 s). In a step it fails the step; in the cleanup or a leftover probe it is added to `cleanup.errors`, the remaining cleanup actions
still run and the JSON summary is still written (`BoundedTest`, with a tmux shim that hangs).

Session ownership is tested in fake mode with the shim (`SessionOwnershipTest`; the shim reads `G3_SHIM_RIVAL`, `G3_SHIM_HANG`, `G3_SHIM_AFTER` and `G3_SHIM_MATCH`): a rival session of the same name makes
`new-session` answer `duplicate session` (nothing killed, the rival and its pane remain, reported); the creation succeeds in tmux and then the client hangs (timeout, marker is the run's, marker read back, then
the session is killed and the run is clean); the creation hangs while a rival session with another marker exists (timeout, marker differs, session kept and reported in `cleanup.kept`); the creation hangs and
no session appears (nothing killed). `GuardTest` adds that the cleanup runs no `branch -D`, `worktree remove` or `lane remove --abandon` with nothing recorded, kills a recorded pane only if it is in the run's
session, and that run ids never collide and every name is a valid lane name.

## `--fake` and the tmux isolation rules

`--fake` replaces only the model-facing parts: the tmux calls that would start a worker (a sleeper pane instead), the worker's replies, the reviewer's
verdict, and `tp-route --review-plan` (a copy of `bin/` with a fixed plan: reviewer `openai/gpt-6-sol`, different provider). The tools are the real ones.
It makes a tiny temp repo when `--repo` is not given. Test hooks (fake only): `--inject-failure STEP`, `--tamper NAME` (the names are the keys of `TAMPERS` in the script).

The owner's tmux server is never reachable from a fake run:
- the constructor makes a fresh `TMUX_TMPDIR` (`/tmp/g3t-*`) and a private socket name, and `-L <socket>` is part of the tmux command from then on;
  every subprocess gets that `TMUX_TMPDIR`; `TMUX` and `TMUX_PANE` are removed (live mode keeps the caller's `TMUX_TMPDIR`, drops those two);
- `kill-server` runs only in fake mode, only with `-L <this run's socket>`, only with `TMUX_TMPDIR` equal to the run's directory (asserted in code first),
  and only when the private socket exists. Live mode never runs it; it kills only the pane ids its tasks recorded (and only if tmux lists them in the run's session) and the session it made (see the ownership rule above).

The tests put a recording `tmux` shim first on PATH: it logs argv and env of every call, refuses (exit 99, real tmux not run) any call without a
private `g3t-*` `TMUX_TMPDIR` or with `TMUX`/`TMUX_PANE` set, and any `kill-server` not on a `-L g3-*` socket. The test process carries a decoy `TMUX_TMPDIR`, `TMUX` and `TMUX_PANE`.
After every test, and after the module, an audit compares each recorded call with the run that made it (the run's JSON summary names its `tmux_tmpdir` and `socket`): the call's `TMUX_TMPDIR` must be that
run's own, its `-L` socket that run's own, each `kill-server` that run's own and at most once. It is equality with the run's own values, not a prefix match, and it has its own test (`test_the_audit_compares_with_each_runs_own_socket_and_dir`).
The decoy directory must stay empty.

## What can reach a model

Only the live command above (`g3_e2e.py` without `--fake`). The tests, `--fake`, `--inject-failure` and `--tamper` start no worker, reviewer or model.
Scripts written: `tools/smoke/g3_e2e.py`, `tests/g3_e2e_support.py`, `tests/test_g3_e2e.py`, `tests/test_g3_e2e_tamper.py`, and two one-off scratch scripts outside the repository: a probe of the three hooks against
candidate trigger commands (`probe.py`) and a mutation check that runs the new tests against deliberately broken copies of the script (`mutate.py`; every mutation made its tests fail).
Run: the two test modules in four cells (python 3.9 and 3.14, each with the session environment set and unset), and `g3_e2e.py --fake` directly.
