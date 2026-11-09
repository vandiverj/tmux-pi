# G3: the end-to-end smoke script

`tools/smoke/g3_e2e.py` drives the real `bin/` tools as subprocesses through the eight G3 steps
(dispatch, a declined permission prompt with a redirect, retry, cross-provider review, `lane close`, merge, `collect`, `lane remove`),
then checks that nothing of the run is left. Python 3.9, standard library only. Tests: `tests/test_g3_e2e.py`.

## The live command (Main runs it; the lane never did)

From a tmux-pi checkout that has this script, inside the tmux server that Main uses (the default `TMUX_TMPDIR` is kept):

    python3 tools/smoke/g3_e2e.py --repo ~/Documents/Projects/battle-soccer --out .tmux-pi/g3/live-$(date -u +%Y%m%d%H%M%S).json

Exit 0: every step and the cleanup passed. Exit 1: stderr says `FAILED at step '<name>': <what>`, and `cleanup: ...` / `left behind: ...` lines.
`--out` gets the JSON summary (run, ok, failed_step, error, per-step name / rc / seconds / asserted list / commands, cleanup report, task ids).
Useful flags: `--keep` (keep the temp state dir), `--coder-model` (default `claude-haiku-5-5`), `--prompt-wait 240`, `--settle-wait 600`, `--review-wait 600`.

What it makes, all named `g3-smoke-<UTC stamp>`: a tmux session, a lane (worktree `lane/g3-smoke-<stamp>` of battle-soccer), and one scratch
integration branch `lane/integrate` (created at battle-soccer's current HEAD; the script refuses to start if it already exists, and deletes it at the end).
battle-soccer's own branch never moves (asserted). The state dir (task records) is a temp dir of the run, not Main's.

## Cost (live)

One coder task that is declined and redirected (it makes no change), one retry coder task (adds one file), one review. All small:
claude-haiku-5-5 at low thinking for both coder tasks, and the reviewer tp-route picks (it must be a different provider). No other model call.

## The permission prompt: mechanism, and how deterministic it is

The first coder task says: read `.env.local` in the repo root with the Read tool, then (after that) create `G3_SMOKE.md`.
`.env.local` is a credential-shaped name (`lib/sensitive_read.py`, `_STRONG`/`_FILE_SAMPLES`); in a worker, the redaction hook
(`lib/redact_hook.py`, `save_request`) answers a Read of such a file with `ask` and writes `permission-request.json` with
`status: pending` in the task dir; `tp-agent inspect` then reports `status: needs_input` with a `permission` block (`lib/tp_detail.py`).
The file is never created, so nothing is read even if the prompt were accepted. The script then does what Main does: sends `Escape` to the
task's pane (declines the dialog; the hook's PermissionDenied event sets the request to `denied`), waits for the request to leave `pending`,
and sends the redirect with `tp-agent message` ("do not read it, make no change, reply RESULT_STATUS: incomplete").

Determinism: the trigger is the file name and the hook, not the model's judgement, but the model still has to follow the instruction and make the
Read call, so it is not 100%. The script never hangs on this: it waits at most `--prompt-wait` seconds for `needs_input` + pending, and fails at
step `prompt` with "no permission prompt ... within N s (last seen: ...)"; it fails at once if the task is already settled, failed or stopped
(a worker that finished without asking will not ask). If the live run shows the prompt is flaky, raise `--prompt-wait` or rerun; the lane could not check it live.

## What each step asserts (a failed assertion fails the step and names itself)

- setup: the repo is a git checkout with no uncommitted tracked change; `lane/integrate` and `lane/<name>` do not exist yet; the tmux session starts.
- dispatch: a `coder-` task id; started (not queued); `meta.json` names the lane and step `s1`; a pane is recorded; the lane record is `open`, branch
  `lane/<name>`, based on the scratch integration branch; the lane worktree exists.
- prompt: task state `needs_input`; `permission-request.json` is `pending` and names `.env.local`.
- decline: after `Escape` the request leaves `pending` and is not `authorized`; the redirect is sent; the task settles with `result_status: incomplete`;
  the lane worktree is still unchanged.
- retry: the first task is stopped (a lane allows one writer); `retry` starts a new task that keeps the lane and step; it settles with `result_status: complete`;
  the worktree holds exactly the one new file, `G3_SMOKE.md`, whose whole content is the requested line.
- review: `tp-review` prints a reviewer task id; `review-of` names the reviewed task; the reviewer's provider differs from the author's and the plan's
  independence is `different_provider`; `reviewed-state.json` names the reviewed tree; the reviewer settles with an approving verdict
  (`APPROVED` / `APPROVED_WITH_NOTES`) and a complete review (Coverage section).
- close: the file is committed on the lane branch, the tree is clean; after `tp-agent lane close` the lane record lists step `s1` as closed.
- merge: a merge commit on `lane/integrate` (made with `commit-tree`, no checkout) with parents (previous tip, lane tip); the branch changed exactly one file,
  added; that file holds exactly the requested line; the repository's own branch did not move.
- collect: `collect` of the retry and of the reviewer returns their records, the tasks are `completed` and have their completed marker; the panes of all three tasks are gone.
- remove: `lane remove` says removed, not abandoned, integrated in `lane/integrate`; the lane worktree directory is gone; the lane record is archived.
- leftovers: no worktree, branch, lane record or pane of the run is left; after killing the run's session, no session and no pane of it remains.

A `finally` block always runs the cleanup (kill the run's recorded panes and its own session by id/name, `lane remove --abandon`, `git worktree remove`,
delete `lane/<name>` and `lane/integrate`, remove the temp dir), then re-checks the leftovers. Every wait is bounded, with an error that names what was waited for.

## `--fake` and the tmux isolation rules

`--fake` replaces only the model-facing parts: the tmux calls that would start a worker (a sleeper pane instead), the worker's replies, the reviewer's
verdict, and `tp-route --review-plan` (a copy of `bin/` with a fixed plan: reviewer `openai/gpt-6-sol`, different provider). The tools are the real ones.
It makes a tiny temp repo when `--repo` is not given. Test hooks: `--inject-failure STEP`, `--tamper task-state|lane-record|review-verdict|scratch-commit`.

The owner's tmux server is never reachable from a fake run:
- the constructor makes a fresh `TMUX_TMPDIR` (`/tmp/g3t-*`) and a private socket name, and `-L <socket>` is part of the tmux command from then on;
  every subprocess gets that `TMUX_TMPDIR`; `TMUX` and `TMUX_PANE` are removed (live mode keeps the caller's `TMUX_TMPDIR`, drops those two);
- `kill-server` runs only in fake mode, only with `-L <this run's socket>`, only with `TMUX_TMPDIR` equal to the run's directory (asserted in code first),
  and only when the private socket exists. Live mode never runs it; it kills only the pane ids its tasks recorded and the session it created.

`tests/test_g3_e2e.py` puts a recording `tmux` shim first on PATH: it logs argv and env of every call, refuses (exit 99, real tmux not run) any call without a
private `g3t-*` `TMUX_TMPDIR` or with `TMUX`/`TMUX_PANE` set, and any `kill-server` not on a `-L g3-*` socket. The test process carries a decoy `TMUX_TMPDIR`, `TMUX` and `TMUX_PANE`;
every test and the module audit that no call used them and that the decoy directory stayed empty.

## What can reach a model

Only the live command above (`g3_e2e.py` without `--fake`). The tests, `--fake`, `--inject-failure` and `--tamper` start no worker, reviewer or model.
Scripts written: `tools/smoke/g3_e2e.py`, `tests/test_g3_e2e.py`. Run: the test module in four cells, and `g3_e2e.py --fake` directly.
