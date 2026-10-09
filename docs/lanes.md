# Lanes

A lane is a git worktree and branch that one stream of work owns, so two streams can change different paths at once without sharing a working tree. The design (one writer per lane, a review gate between steps) is described in the [README](../README.md#lanes); this page is the command reference, the configuration, and the one table of every refusal. Every command below was run in a scratch repository with a private state directory; paths are shortened to `/tmp/demo`.

## When to use a lane

Use one when two or more substantial streams of work change different paths at the same time and each needs more than one worker (write, review, fix). Do small or single-worker work in the main checkout.

## Commands

### `tp-agent lane create NAME --domain TEXT --touches PATH... [--repo DIR] [--base REF]`

Makes the branch (`lane/NAME`), the worktree (`/tmp/demo-wt-NAME`) and the lane record. `--domain` says in words why the lane is independent; `--touches` lists the repo-relative path prefixes it will change (no trailing `/`). The base is the integration branch (`lane/integrate`) unless `--base` is given; the branch must exist (`git branch lane/integrate main`). Nothing is left behind if it fails.

```
$ tp-agent lane create api --domain "HTTP handlers" --touches app
{
  "lane": {
    "schema": "tp-lane:v1",
    "name": "api",
    "kind": "work",
    "state": "open",
    "repo": "/tmp/demo",
    "git_common_dir": "/tmp/demo/.git",
    "branch": "lane/api",
    "worktree": "/tmp/demo-wt-api",
    "base": { "ref": "lane/integrate", "commit": "5f7b64aca0f4..." },
    "created": "2026-10-09T04:26:55+00:00",
    "domain": "HTTP handlers",
    "touches": [ "app" ],
    "closed": [],
    "integrated": null,
    "defaulted": [ "branch_prefix", "integration_branch", "integration_lane", "max_active_lanes", "worktree_pattern" ]
  },
  "warnings": []
}
```

`defaulted` lists the configuration keys that were absent and took their default (see Configuration).

### `tp-agent dispatch --lane NAME --step ID --role ROLE --task TEXT [--tests affected|none]`

Runs a task in the lane's worktree. `--step` is the slice the task belongs to (letters, digits, `.`, `_`, `-`, up to 40). A lane that does not exist yet is created from `--domain` and `--touches` in the same call (`"lane_created": true` in the output); for an existing lane those flags are refused, because the declaration is set when the lane is created. `--workdir` is refused (the lane chooses the tree), and so is `--tests full`. `retry` and `tp-agent review` copy the lane and step of the task they follow.

```
$ tp-agent dispatch --lane api --step s1 --role coder --task "Add GET /health" --tests affected
{
  "id": "coder-364bc24f72",
  "role": "coder",
  "status": "starting",
  ...
  "lane": "api",
  "lane_step": "s1",
  "lane_created": false
}
```

**One writer.** A lane takes one writer at a time; any number of reviewers may share it, but not with a writer. Starting anything in the lane's working tree without `--lane` is refused. Never start a worker by hand there.

**Steps.** The next step cannot start until the previous one is closed (below); a retry or a fix inside an open step is never held back.

```
$ tp-agent dispatch --lane api --step s2 --role coder --task "More" --tests affected
tp-agent: lane api: step s1 is not closed (coder-364bc24f72 is running, so the step's work is not finished. Wait for it to finish, or end it with `tp-agent stop coder-364bc24f72`). The next step starts after the review of s1 finishes and `tp-agent lane close api s1` has recorded it
```

### `tp-agent lanes [NAME] [--repo DIR]`

Read-only list of the repository's lanes (JSON), including worktrees git knows that no lane record names. Per lane: `name`, `kind`, `state`, `repo`, `branch`, `worktree`, `base`, `domain`, `touches`, `created`, `age`, `open_step`, `steps_closed`, `occupant`, `queued`, `waiting_on`, `ahead` (commits past the base), `dirty` (uncommitted paths), `last_closed`. `open_step`, `occupant`, `queued` and `waiting_on` are read from this session's tasks, so run it in the workspace (or with `TP_STATE_DIR` set); without a session it prints a warning and shows them as null.

```
$ tp-agent lanes api
[
  {
    "name": "api", "state": "open", "branch": "lane/api", "worktree": "/tmp/demo-wt-api",
    "created": "2026-10-09T04:26:55+00:00", "age": "0s",
    "open_step": "s1", "steps_closed": 0, "occupant": null, "queued": null,
    "waiting_on": "worker", "ahead": 0, "dirty": 0, "last_closed": null, ...
  }
]
```

`waiting_on` says who moves next:

| `waiting_on` | Meaning | You do |
| --- | --- | --- |
| `worker` | a task of the open step is queued or running | wait, answer it (`tp-agent message ID --text "..."`), or `tp-agent stop ID` |
| `review` | the step has no usable review of its current tree, or one is running | wait for the running one, or `tp-agent review AUTHOR --force` |
| `orchestrator-dispatch` | no open step, a review asked for changes, or no writer is left to review | `tp-agent dispatch --lane NAME --step ID ...` (a new step, or a task in the open step), or `tp-agent retry AUTHOR --reason "..."` |
| `orchestrator-commit` | the review approved work that is not committed | `git -C WORKTREE add -A && git -C WORKTREE commit -m "..."` |
| `orchestrator-close` | the step is committed and approved (or changed nothing) | `tp-agent lane close NAME STEP` |
| `none` | the lane is not open | nothing; start a new lane |

The same tags appear on each task: `lane` and `lane_step` in `tp-agent list`, in `tp-agent index` (the `active`, `pending` and `recent` rows), and the index has a `lanes` summary (tasks, status counts and steps per lane).

### `tp-agent lane close NAME STEP [--repo DIR]`

Records that a step is finished: its work is committed in the lane and a review of exactly that committed tree approved it. It appends (step, commit, tree, review, verdict) to the lane's `closed` list and prints the record. Nothing skips or overrides the gate. A step that left the tree as the last closed commit had it needs no review and is recorded with verdict `EMPTY`. Run it in the session that ran the step. The rules for what counts as a review are in the [README](../README.md#lanes).

```
$ tp-agent lane close api s1
{ ...
  "closed": [
    { "step": "s1", "commit": "5f7b64aca0f4...", "tree": "bfa24c815ad1...", "review": null, "verdict": "EMPTY", "closed_at": "2026-10-09T04:28:01+00:00" }
  ], ... }
$ tp-agent lane close api s1
tp-agent: step s1 is already closed at 5f7b64aca0f46d2585e35feda00b676a46252162; it is recorded, so there is nothing to do. The next step takes a new id: `tp-agent dispatch --lane api --step NEW-ID --role ROLE --task "..."`
```

Closing a step that is not finished says why and what to run:

```
$ tp-agent lane close docs d1
tp-agent: step d1 of lane docs cannot be closed: step d1 has no review yet. No writer of the step can be reviewed (none is settled in this session's active tasks). Dispatch a task into the step with `tp-agent dispatch --lane docs --step d1 --role coder --task "..."`, and review that one
```

### `tp-agent lane remove NAME [--abandon] [--repo DIR]`

Removes the lane's worktree and branch and archives its record under `<git common dir>/tmux-pi/lanes/archive/`. A lane whose branch tip is already in the main checkout's current branch or in `lane/integrate` goes without `--abandon`; any other needs it, and the tip and the command that restores the branch are printed.

```
$ tp-agent lane remove docs
tp-agent: lane docs is open with 0 closed step(s) not integrated (its tip f5363c774082 is in neither the main checkout's branch nor lane/integrate); integrate it or pass --abandon: `tp-agent lane remove docs --abandon --repo /tmp/demo` discards the branch after printing its tip, which restores it
$ tp-agent lane remove docs --abandon
tp-agent: lane docs is abandoned with its work unmerged; its branch lane/docs was at f5363c774082849f1949b144048d0e780549b90b. To get it back: git -C /tmp/demo branch lane/docs f5363c774082849f1949b144048d0e780549b90b
{
  "lane": "docs",
  "removed": true,
  "abandoned": true,
  "integrated_in": [],
  "worktree": "/tmp/demo-wt-docs",
  "branch": "lane/docs",
  "tip": "f5363c774082849f1949b144048d0e780549b90b",
  "restore": "git -C /tmp/demo branch lane/docs f5363c774082849f1949b144048d0e780549b90b",
  "archived": "/tmp/demo/.git/tmux-pi/lanes/archive/docs-20261009T042829Z"
}
```

## Configuration

Every key is optional: an absent file, block or key takes its default, and the lane record lists what was defaulted. A key that is present but malformed refuses, naming the key and the file.

Machine-wide, `config/orchestration.json`:

```json
{
  "lanes": { "max_active_lanes": 2 }
}
```

Per repository, `.tmux-pi/lanes.json` in the main checkout (values shown are the defaults except `owned`):

```json
{
  "branch_prefix": "lane/",
  "worktree_pattern": "{repo}-wt-{name}",
  "integration_branch": "lane/integrate",
  "integration_lane": "integrate",
  "owned": ["README.md", "docs"]
}
```

| Key | Meaning |
| --- | --- |
| `max_active_lanes` | the most lanes open at once (positive integer) |
| `branch_prefix` | a lane's branch is `branch_prefix` + name |
| `worktree_pattern` | where the worktree goes; `{repo}` is the repository folder name, `{name}` the lane name (once); it starts with `{repo}` or is absolute |
| `integration_branch` | the default base of a new lane; also where `lane remove` looks for integrated work |
| `integration_lane` | the lane name reserved for integration |
| `owned` | path prefixes only the orchestrator edits; a lane's `--touches` may not overlap them |

The repository's `.tmux-pi/lanes.json` also accepts a `tests` entry; it is validated but nothing uses it yet.

## Lane prompts and refusals

Each refusal names the command that clears it. "Clears itself" means the state goes away without a command, such as a holder finishing. Commands are copied from the [README](../README.md#lane-recovery) tables.

| Message | What it means | Clears itself? | Clear it with |
| --- | --- | --- | --- |
| `lane NAME already has a writer: TASK (writer, running, pid P, session S; …)` or `lane NAME is in use by a reviewer: TASK (reviewer, …)` | another task holds the lane. A reviewer shares the lane with other reviewers but excludes a writer | Yes, when the holder's process ends | `running`: wait, or end it with the command the message prints. From another tmux session that command is `env -u TMUX -u TMUX_PANE -u TP_SESSION TP_STATE_DIR=S tp-agent stop TASK`, which works as printed in any session. The lane is free once the process is gone |
| the same with `settled: finished, not collected` | the holder has finished and its pane is still alive: waiting never frees the lane | No | `tp-agent collect TASK` (ends the process), or `tp-agent stop TASK`. `tp-agent reap` does not list a settled task |
| the same with `wait: TASK is still starting` | a `tp-spawn` is mid-start (a few seconds) | Yes (a few seconds) | wait and dispatch again. If it is still there after a minute, nothing runs from it: the next dispatch drops it by itself |
| the same with `kill PID` or `tmux kill-pane -t %N` | a `tp-spawn` was killed after it made the pane; the pane still runs, so the lane stays held | No | run that command; the lane is free once the pane's process ends |
| `pane %N of TASK could not be ended; its task directory and lane NAME stay as they are` | a failed start could not remove its own pane | No | `tmux kill-pane -t %N`, then dispatch again |
| `TASK has no readable meta.json and a process of it is running` | a start was killed after it made its task directory, and its pane runs | No | the command in the message; a task directory with no `meta.json` and nothing running is only a warning (`rm -r DIR` when you no longer need it) |
| `lease ... is not valid JSON` / `not a tp-lane-lease:v1 lease` | the lease file is damaged and might hide a writer, so it is not used | No | check nothing runs on the lane (`tp-agent lanes`, `tmux list-panes -a`), then `rm` the file named in the message; the lane starts with no holder |
| `queue entry X.json cannot be read` | a queued item the lane cannot rule out as a holder | No | `tp-agent stop X` removes it (a file whose name is not a task id is deleted by hand: the message says so) |
| `TASK has no recorded working tree` / `cannot read TASK/meta.json` / `records an unreadable pane` | a task of this session whose record is damaged and that might be running | No | if it runs (`tmux list-panes -a`), end it: `tmux kill-pane -t %N`. Otherwise add `"workdir": "PATH"` to its `meta.json` (or fix the pane id) and dispatch again |
| `cannot read tmux panes` / `cannot read the process table` | tmux or `ps` could not be asked, so a writer cannot be ruled out | Yes, once the table reads | run `tp-agent` from inside the tmux session (`tmux list-panes -a` must work there) and dispatch again; it clears itself once the table reads |
| `lane NAME has an unreadable record PATH` | the lane's `lane.json` does not parse or belongs to another repository | No | `python3 -m json.tool PATH` names the broken line; fix it, comparing with a healthy `lane.json` of another lane. Or retire the lane (below) |
| `worktree W does not exist` / `is on X, expected BRANCH` / `is not registered with git` | the lane's tree was moved, deleted or switched by hand | No | `git -C W switch BRANCH` for a switched tree; for a missing or unregistered one retire the lane (below) and make a new one |
| `lane NAME: step S is not closed (…)` (a dispatch into another step) or `step S of lane NAME cannot be closed: …` (`lane close`) | the open step is not finished. The text after it says which part and names the command | Partly: a running worker or review ends by itself, the rest needs the command | in this order: a worker still running: wait, or `tp-agent stop TASK` (`tp-agent message TASK --text "..."` if it is waiting for an answer); no review yet, a review of another tree, or none usable: `tp-agent review AUTHOR --force`; a review running: wait, or `tp-agent stop REVIEW`; a review that asked for changes: change the work (`tp-agent retry AUTHOR --reason "..."`) and review the new tree; approved but not committed: `git -C WORKTREE add -A && git -C WORKTREE commit -m "..."`; approved and committed, or nothing changed since the last closed step (no review needed): `tp-agent lane close NAME S`. With no author left to review (its task was archived or never settled): `tp-agent dispatch --lane NAME --step S --role ROLE --task "..."` |
| `step S of lane NAME was closed at COMMIT; start a new step id` | a task for a step that is already recorded as finished (a retry of one included) | No | dispatch with a new `--step` id; the new step starts from the closed commit |
| `lane NAME has changes since step S closed` (or `since the lane's base`) | the lane's tree has commits or uncommitted work that no closed step accounts for, made outside the tool | No | run the commands in the message, in order: `git -C WORKTREE stash push -u -m "..."` for uncommitted work, `git -C WORKTREE branch saved/NAME-SHA HEAD && git -C WORKTREE reset --hard CLOSED` for commits (they stay on the `saved/` branch); then dispatch again. To keep that work in the lane, cherry-pick it from the stash or the `saved/` branch inside the next step's task |
| `step S is already closed at COMMIT` / `step S is not lane NAME's open step (O, the oldest that is not closed)` / `lane NAME has no step S in this session` (from `lane close`) | `lane close` was given a step that is recorded, is not the oldest open one, or belongs to another session | No | close the step the message names (`tp-agent lane close NAME O`); `tp-agent lanes NAME` lists the steps; from another session run it with that session's `TP_STATE_DIR`. A step that changed nothing needs no review: `tp-agent lane close NAME S` records it as `EMPTY` |
| `TASK (DIR) belongs to step S but its meta.json cannot be read` (from a dispatch into a new step, or `lane close`) | a task the lane names has a damaged or missing `meta.json`, so what it did cannot be judged and its step stays open | No | the command in the message: if a process still runs from it, `tmux kill-pane -t %N` (or `kill PID`) first, then `rm -r DIR`; or repair `meta.json` by hand. Then the step is judged by what is in the tree, and the commands of the next refusal apply |
| `closed step S commit C is not on branch B` | the lane's branch was reset or rewritten below a recorded step | No | run the command in the message (it keeps what is on the branch now under `saved/NAME-SHA` and puts the branch back on C). If C no longer exists, or `records tree … lane.json was changed by hand`, or `does not follow`, the record cannot be trusted: retire the lane (below) |
| `N lanes are open (limit M)` | `lanes.max_active_lanes` is reached | No | retire a lane you no longer need (below), or raise `lanes.max_active_lanes` in `config/orchestration.json` |
| `lane NAME is integrating` / `integrated` / `abandoned` | the lane takes no more work | No | dispatch into a new lane with another name |
| a malformed `lanes` block in `config/orchestration.json` or the repository's `lanes.json` (the message names the key and file) | the configuration cannot be used | No | set the named key to the value the message states, in the named file |
| `invalid lane name` / `needs --step` / `--lane chooses the working tree; remove --workdir` / `lane NAME does not exist; pass --domain ... --touches ...` / `already exists ... its declaration is set when it is created` / `--touches X is owned by the orchestrator` | a flag is missing, wrong or not allowed | No | run the same command with the flag change the message names |
| `lane tasks run the tests that check their change` | `--tests full` in a lane | No | pass `--tests affected` or `--tests none`; run the whole suite once from the main checkout |
| `X is inside lane NAME's working tree; pass --lane NAME --step ID` | a start in a lane's tree without the lane | No | repeat it with `--lane NAME --step ID` (or start it in the main checkout) |
| `lane NAME is STATE with N closed step(s) not integrated` (from `lane remove`; STATE is any recorded state) | the lane's branch tip is in neither the main checkout's current branch nor `lane/integrate`; removing would drop its work. The recorded state alone never refuses | No | `tp-agent lane remove NAME --abandon` (prints the tip and the command that restores the branch), or merge the lane into the main branch or `lane/integrate` first |
| `lane NAME has a live process: TASK (…; to end it: CMD)` (from `lane remove`) | a worker still runs in the lane | Yes, when the worker ends | run `CMD` as printed, then remove again |
| `lane NAME has queued work: ID, ID` (from `lane remove`) | queued items, in this session or any other session of the same state root, would start in the lane | No | run each `tp-agent stop ID` as printed (an item of another session comes with `env ... TP_STATE_DIR=THAT_SESSION`), then remove again |
| `lane NAME's tree has N uncommitted path(s)` (git's message is in the text) | tracked changes or untracked files that git does not ignore; ignored build output never blocks | No | `git -C WORKTREE stash push -u -m "..."` (keeps them), then remove again |
| `waiting for lane NAME's writer TASK` (tick, queued) | not a refusal: the item starts when the writer is gone | Yes | nothing, or `tp-agent stop TASK` / `tp-agent stop ITEM` |

## Removing a lane

`tp-agent lane remove NAME [--abandon]` removes the lane's worktree (`git worktree remove`, never `--force`; git-ignored build output goes with it), deletes its branch and moves its record to `<git common dir>/tmux-pi/lanes/archive/NAME-TIMESTAMP/`. It only touches the worktree its record names and git registered. A lane may go when its branch tip is already in the main checkout's current branch or in `lane/integrate` (the tip decides, never the recorded state: no state is a dead end), or, with `--abandon`, in any state: then `git branch -D` runs after the tip and the restoring command (`git -C REPO branch BRANCH SHA`) are printed. A tip that is already integrated has its branch deleted with `git update-ref -d refs/heads/BRANCH TIP` (it deletes only while the branch is still at that tip; no `-D`, no force), because `git branch -d` from the main checkout refuses a branch merged only into `lane/integrate`. Refusals are in the table above. `tp-agent lanes` shows each lane's `created` time and `age`.

## Retiring a lane by hand

If a lane's record or tree is damaged so that `lane remove` cannot read it, drop it by hand. To drop a lane that is no longer wanted (this discards its unmerged work; look at `tp-agent lanes` first): end its tasks (`tp-agent stop ID`), then `git -C REPO worktree remove WORKTREE` (add `--force` only when you have kept what is in it), `git -C REPO branch -D BRANCH`, and `rm -r GIT_COMMON_DIR/tmux-pi/lanes/NAME`. REPO, WORKTREE and BRANCH are in the lane's row of `tp-agent lanes`; GIT_COMMON_DIR is `git -C REPO rev-parse --git-common-dir`.

## Not built yet

- overlap warnings between lanes, integrating lanes (`tp-agent integrate`), `push-check`, and lane setup, clone and disk limits.
- Lanes are merged by hand today: `git merge` each lane's branch into the integration branch, then run the whole test suite once from the main checkout. `--tests full` is refused inside a lane for that reason.
