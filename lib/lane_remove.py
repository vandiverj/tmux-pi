"""Lanes L6: `tp-agent lane remove NAME [--abandon]` (design 3.5).

A lane may go when its work is already in the repository: its branch tip is an ancestor of the main checkout's HEAD or of the
configured integration branch (integration_branch in the repository's lanes.json, lane/integrate unless set). The tip decides, never the recorded state: integration, L7, does not exist yet,
and a state that is only a word (open, integrating, integrated, abandoned) must not be a dead end. A lane whose tip is in
neither is removed only with --abandon, whatever its state. Under the lane lock, in this order:

  1. the record is read and checked against git (lanes.lane_reality), so the worktree removed is the one the record names and
     git registered, never a path found any other way;
  2. refusals: tip not integrated; a live process on the lane; queued work for it in any session of this state root; then git's
     own refusal of a dirty tree;
  3. `git worktree remove` (never --force: tracked changes and untracked files that git does not ignore stop it; ignored build
     output does not, and goes with the tree);
  4. the branch: a tip that is integrated is deleted with `git update-ref -d refs/heads/BRANCH TIP`, which deletes only while
     the branch is still at the tip our ancestry check proved integrated (no -D, no force; `git branch -d` would test the tip
     against HEAD or the upstream only, and so refuse a branch that is merged into the integration branch); with --abandon and an
     unintegrated tip, `git branch -D`, after the tip and the command that restores the branch were printed;
  5. the lane's directory is moved to lanes/archive/NAME-TIMESTAMP/.

Nothing before step 3 changes anything. If step 4 fails after the worktree is gone, the record is still archived (a record whose
tree is gone only refuses everything) and the failure names the command that finishes the job.

Queued work is looked for in this session and in every sibling session directory (STATE_ROOT/sessions/*) when the state
directory is one of them; the lane's lease is shared by every session and covers work that has started."""
import os
from pathlib import Path
import shlex
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lane_commands  # noqa: E402
import lane_lease  # noqa: E402
import lane_steps  # noqa: E402
import lanes  # noqa: E402

def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _is_ancestor(common, tip, ref):
    return lanes._git(["merge-base", "--is-ancestor", tip, ref], common, ok=(0, 1))[0] == 0


def _integrated_in(repo, common, tip, integration_branch):
    """The names of the refs that already hold `tip`: the main checkout's current branch (or its commit), and `integration_branch`."""
    found = []
    _, head = lanes._git(["rev-parse", "HEAD"], repo)
    _, head_branch = lanes._git(["symbolic-ref", "-q", "--short", "HEAD"], repo, ok=(0, 1))
    if _is_ancestor(common, tip, head.strip()):
        found.append(head_branch.strip() or f"HEAD {head.strip()[:12]}")
    code, integration = lanes._git(["rev-parse", "--verify", "-q", f"refs/heads/{integration_branch}^{{commit}}"], repo, ok=(0, 1))
    if code == 0 and _is_ancestor(common, tip, integration.strip()):
        found.append(integration_branch)
    return found


def _sessions(state):
    """This session's state directory and, when it is one of STATE_ROOT/sessions/*, every sibling that has been used."""
    state = Path(state)
    found = [state]
    if state.parent.name == "sessions" and state.parent.is_dir():
        found += [p for p in sorted(state.parent.iterdir()) if p.is_dir() and not p.name.startswith(".") and p != state]
    return found


def _queued(state, name):
    """[(id, session directory)] of the queued items of lane `name` in every session (oldest first within a session)."""
    out = []
    for session in _sessions(state):
        items = [q for q in lane_steps.Session(session).queued if q["lane"] == name]
        out += [(q["id"], session) for q in sorted(items, key=lambda q: q["when"])]
    return out


def _stop_command(ident, session, here):
    return f"`{lane_lease.in_session({'session': str(session)}, here)}tp-agent stop {ident}`"


def _unmerged_refusal(record, repo, tip, integration_branch):
    name, state = record["name"], record["state"]
    return (f"lane {name} is {state} with {len(record['closed'])} closed step(s) not integrated (its tip {tip[:12]} is in neither "
            f"the main checkout's branch nor {integration_branch}); integrate it or pass --abandon: "
            f"`tp-agent lane remove {name} --abandon --repo {repo}` discards the branch after printing its tip, which restores it")


def _delete_integrated_branch(repo, branch, tip):
    """Delete `branch` while it is still at `tip`, which was proven to be in the repository (no -D)"""
    lanes._git(["update-ref", "-d", f"refs/heads/{branch}", tip], repo)
    lanes._git(["config", "--remove-section", f"branch.{branch}"], repo, ok=(0, 128))  # the settings `git branch -d` removes too


def _tree_refusal(record, repo, error):
    name, worktree = record["name"], record["worktree"]
    try:
        count = lane_steps._dirty(worktree)
    except ValueError:
        count = 0
    if count:
        return (f"lane {name}'s tree has {count} uncommitted path(s) ({error}). Set them aside with "
                f"`git -C {worktree} stash push -u -m \"lane {name}: before remove\"` (the stash keeps them), then remove again")
    unlock = f"; unlock it with `git -C {repo} worktree unlock {worktree}`, then remove again" if "locked" in str(error) else ""
    return f"git would not remove lane {name}'s worktree ({error}){unlock}"


def remove_lane(state, repo_dir, name, abandon=False, notify=None):
    """Remove lane `name` (see the module text). `state` is the session state directory (for queued work); `notify(text)` is
    called with the abandoned tip and its restore command before the branch is deleted. Returns what was done."""
    common, repo = lane_commands._main_repo(repo_dir)
    lane_commands.check_lane_name(name)
    if name not in lanes.lane_names(common):
        raise ValueError(f"there is no lane {name} in {lanes.lanes_dir(common)}; `tp-agent lanes --repo {repo}` lists the lanes")
    notify = notify or (lambda text: print(f"tp-agent: {text}", file=sys.stderr))
    with lanes.lane_lock(common):
        record = lanes.read_lane(common, name)
        tip = lanes.lane_reality(record)["tip"]  # also proves the worktree is a linked one git registered: never the main tree
        worktree, branch = record["worktree"], record["branch"]
        if worktree == repo:
            raise ValueError(f"lane {name}'s worktree is the repository's main working tree; it is not removed")
        integration_branch = lanes.load_repo_config(repo)["integration_branch"]
        where = _integrated_in(repo, common, tip, integration_branch)
        merged = bool(where)
        if not merged and not abandon:
            raise ValueError(_unmerged_refusal(record, repo, tip, integration_branch))
        holders = lane_lease.live(lane_lease.read(common, name))
        if holders:
            raise ValueError(f"lane {name} has a live process: {lane_lease.describe(holders, state)}. Once it is gone, remove the lane again")
        queued = _queued(state, name)
        if queued:
            stops = ", ".join(_stop_command(ident, session, state) for ident, session in queued)
            raise ValueError(f"lane {name} has queued work: {', '.join(ident for ident, _ in queued)}. Take each out of the queue with {stops}; "
                             f"then remove the lane again")
        try:
            lanes._git(["worktree", "remove", worktree], repo)  # never --force
        except ValueError as error:
            raise ValueError(_tree_refusal(record, repo, error))
        restore = f"git -C {repo} branch {branch} {tip}"
        failure = None
        try:
            if merged:
                _delete_integrated_branch(repo, branch, tip)
            else:
                notify(f"lane {name} is abandoned with its work unmerged; its branch {branch} was at {tip}. To get it back: {restore}")
                lanes._git(["branch", "-D", branch], repo)
        except ValueError as error:
            finish = f"git -C {repo} update-ref -d refs/heads/{branch} {tip}" if merged else f"git -C {repo} branch -D {branch}"
            failure = (f"the worktree {worktree} is removed but the branch {branch} is not ({error}); it is still at {tip}. "
                       f"Delete it with `{finish}` when you are done with it")
        archived = lanes.archive_lane_record(common, name, _stamp())
    if failure:
        raise ValueError(f"lane {name}: {failure} (its record is archived in {archived})")
    return {"lane": name, "removed": True, "abandoned": not merged, "integrated_in": where, "worktree": worktree, "branch": branch, "tip": tip,
            "restore": restore, "archived": str(archived)}
