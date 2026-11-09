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
`--out` gets the JSON summary (run, mode, ok, failed_step, error, socket and tmux_tmpdir (fake only), per-step name / rc / seconds / asserted list / commands,
cleanup report, task ids). Useful flags: `--keep` (keep the temp state dir), `--coder-model` (default `claude-haiku-5-5`), `--prompt-wait 240`,
`--settle-wait 600`, `--review-wait 600`, `--subprocess-timeout 30`.

What it makes, all named `g3-smoke-<UTC stamp>`: a tmux session, a lane (worktree `lane/g3-smoke-<stamp>` of battle-soccer), and one scratch
integration branch `lane/integrate` (created at battle-soccer's current HEAD; the script refuses to start if it already exists, and deletes it at the end).
battle-soccer's own branch never moves (asserted). The state dir (task records) is a temp dir of the run, not Main's.

`--inject-failure` and `--tamper` are test hooks and exist only with `--fake`: without it the script exits 2 before it makes anything
(a live run would start real workers before the hook failed it).

## Cost (live)

One coder task that is declined and redirected (it makes no change), one retry coder task (adds one file), one review. All small:
claude-haiku-5-5 at low thinking for both coder tasks, and the reviewer tp-route picks (it must be a different provider). No other model call.

## The permission prompt: mechanism, and how deterministic it is

The first coder task tells the worker to run exactly one Bash command first (`PROMPT_COMMAND` in the script):

    cat ./<a dotenv-shaped name that does not exist> "$<a secret-shaped variable that is not set>"; exit 3

(both names are built from pieces in the script; they are `.env.g3-smoke-absent` and `G3_SMOKE_API_TOKEN`). Nothing can be read: the file is absent and
the variable is unset, so the command would print an error and exit 3 even if the owner accepted the prompt.

Which hook asks is decided by the redaction mode the worker runs in (`TP_REDACTION` env, else `config/local.json` `redaction.mode`, else `shadow`;
`tp-spawn` does not set it, so the worker inherits Main's mode; Main's is `on`). All three modes ask, decided by code:
- `on`: `bin/tp-redact --claude-hook` (`lib/redact_hook.py` `pre_decision`): a secret-variable reference in a command it cannot wrap (the `exit`) is an `ask`;
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

What is still not 100% deterministic (live only): the worker model must follow the instruction and make that one Bash call. The script never hangs on this: it waits at most `--prompt-wait` seconds for
`needs_input` + pending, and fails at step `prompt` with "no permission prompt ... within N s (last seen: ...)"; it fails at once if the task is already settled, failed or stopped
(a worker that finished without asking will not ask). If the live run shows the model skips the call, raise `--prompt-wait` or rerun.

## What each step asserts (a failed assertion fails the step and names itself)

Each assertion below has a tampered-record case in `tests/test_g3_e2e_tamper.py` (`TAMPER_EXPECT`: tamper name -> step and the check it must name); a cross-check there fails if a check
the passing flow makes has no case.

- setup: the repo is a git checkout (`not-git`) with no uncommitted tracked change (`dirty-repo`); its own branch is not `lane/integrate` (`on-scratch-branch`); `lane/integrate` (`integration-exists`)
  and `lane/<name>` (`lane-branch-exists`) do not exist yet; the tmux session starts.
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

A `finally` block always runs the cleanup (kill the run's recorded panes and its own session by id/name, `lane remove --abandon`, `git worktree remove --force <this run's worktree path>`,
delete `lane/<name>` and `lane/integrate`, remove the temp dir), then re-checks the leftovers. There is no repository-wide `git worktree prune`: other worktrees of the repository,
prunable or not, are never touched (`PruneTest` runs against a repo that has an unrelated prunable worktree). Every wait is bounded, with an error that names what was waited for.

Every subprocess has a timeout: steps use their own (git 60 s, tools 120 s, `tp-review` 180 s), every tmux call, leftover probe and cleanup command uses `--subprocess-timeout` (cleanup git and lane commands: twice that).
A command that times out is killed (a grandchild that holds its pipes is given up after 2 s). In a step it fails the step; in the cleanup or a leftover probe it is added to `cleanup.errors`, the remaining cleanup actions
still run and the JSON summary is still written (`BoundedTest`, with a tmux shim that hangs).

## `--fake` and the tmux isolation rules

`--fake` replaces only the model-facing parts: the tmux calls that would start a worker (a sleeper pane instead), the worker's replies, the reviewer's
verdict, and `tp-route --review-plan` (a copy of `bin/` with a fixed plan: reviewer `openai/gpt-6-sol`, different provider). The tools are the real ones.
It makes a tiny temp repo when `--repo` is not given. Test hooks (fake only): `--inject-failure STEP`, `--tamper NAME` (the names are the keys of `TAMPERS` in the script).

The owner's tmux server is never reachable from a fake run:
- the constructor makes a fresh `TMUX_TMPDIR` (`/tmp/g3t-*`) and a private socket name, and `-L <socket>` is part of the tmux command from then on;
  every subprocess gets that `TMUX_TMPDIR`; `TMUX` and `TMUX_PANE` are removed (live mode keeps the caller's `TMUX_TMPDIR`, drops those two);
- `kill-server` runs only in fake mode, only with `-L <this run's socket>`, only with `TMUX_TMPDIR` equal to the run's directory (asserted in code first),
  and only when the private socket exists. Live mode never runs it; it kills only the pane ids its tasks recorded and the session it created (only if it got as far as creating it).

The tests put a recording `tmux` shim first on PATH: it logs argv and env of every call, refuses (exit 99, real tmux not run) any call without a
private `g3t-*` `TMUX_TMPDIR` or with `TMUX`/`TMUX_PANE` set, and any `kill-server` not on a `-L g3-*` socket. The test process carries a decoy `TMUX_TMPDIR`, `TMUX` and `TMUX_PANE`.
After every test, and after the module, an audit compares each recorded call with the run that made it (the run's JSON summary names its `tmux_tmpdir` and `socket`): the call's `TMUX_TMPDIR` must be that
run's own, its `-L` socket that run's own, each `kill-server` that run's own and at most once. It is equality with the run's own values, not a prefix match, and it has its own test (`test_the_audit_compares_with_each_runs_own_socket_and_dir`).
The decoy directory must stay empty.

## What can reach a model

Only the live command above (`g3_e2e.py` without `--fake`). The tests, `--fake`, `--inject-failure` and `--tamper` start no worker, reviewer or model.
Scripts written: `tools/smoke/g3_e2e.py`, `tests/g3_e2e_support.py`, `tests/test_g3_e2e.py`, `tests/test_g3_e2e_tamper.py`, and a one-off probe of the three hooks (`probe_hooks.py` in the task directory, not in the repository).
Run: the two test modules in four cells (python 3.9 and 3.14, each with the session environment set and unset), and `g3_e2e.py --fake` directly.
