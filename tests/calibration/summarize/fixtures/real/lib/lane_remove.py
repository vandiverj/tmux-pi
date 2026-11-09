"""Lanes L6: `tp-agent lane remove NAME [--abandon]` (design 3.5).

A lane may go when its work is already in the repository: its branch tip is an ancestor of the main checkout's HEAD or of the
configured integration branch (integration_branch in the repository's lanes.json, lane/integrate unless set). The tip decides, never the recorded state: integration, L7, does not exist yet,
and a state that is only a word (open, integrating, integrated, abandoned) must not be a dead end. A lane whose tip is in
neither is removed only with --abandon, whatever its state. Under the lane lock, in this order:

  1. the record is read and checked against git (lanes.lane_reality), so the worktree removed is the one the record names and
     git registered, never a path found any other way;
  2. refusals: tip not integrated; a live process on the lane; queued work for it in any session of this state root; then git's
     own refusal of a dirty tree;
  3. the lane's shared node_modules symlinks (lane_deps) are os.unlink'ed, once the tree is known clean (a tree that git will refuse
     keeps them): a link is never followed, and the main checkout's node_modules is untouched;
     `git worktree remove` (never --force: tracked changes and untracked files that git does not ignore stop it; ignored build
     output does not, and goes with the tree). It is the one git call allowed lanes.REMOVE_TIMEOUT, because unlinking a tree
     with node_modules takes minutes. Just before it, when the tree is clean, lanes/NAME/removing.json is written: it records
     that the tree held nothing of anyone's when the removal began. A removal that is killed or times out can leave the tree
     half deleted; running `lane remove` again then takes the resume path (below) instead of refusing;
  4. the branch: a tip that is integrated is deleted with `git update-ref -d refs/heads/BRANCH TIP`, which deletes only while
     the branch is still at the tip our ancestry check proved integrated (no -D, no force; `git branch -d` would test the tip
     against HEAD or the upstream only, and so refuse a branch that is merged into the integration branch); with --abandon and an
     unintegrated tip, `git branch -D`, after the tip and the command that restores the branch were printed;
  5. the lane's directory is moved to lanes/archive/NAME-TIMESTAMP/.

Nothing before step 3 changes anything but the marker; a removal that cannot tell whether the tree is clean, or cannot write the
marker, stops there and does not start `git worktree remove` (it could not be resumed). Resume path (the marker exists): the checks against git are the history ones
only (lanes.lane_reality(history_only=True)), and the tree is looked at here, on the recorded realpath and nowhere else: a tree
that still has its `.git` link must pass lanes.worktree_identity and show only tracked files deleted (git status " D"); those
are restored with `git restore --worktree --source=HEAD -- .` (the marker proves they were deleted by the interrupted removal,
not by a person's edit) and git removes the tree as usual; anything else in it (untracked, modified, staged) refuses with the
stash command; a tree that is no longer on the recorded branch refuses too. Tracked .gitignore files that the removal deleted
are put back first (here and for a tree without its link), or ignored node_modules would look like someone's untracked files. A tree whose `.git` link is gone is leftovers of that
removal, but the marker only speaks for the moment it was written, so before anything is deleted git is asked (told the tree and
its worktrees/ID entry explicitly) whether the tree is still on the recorded branch and holds only what `git worktree remove`
would have deleted: no path that is untracked and not ignored, no tracked path changed; the entry must not be locked. Any of
those refuses, deleting nothing. Then the tree is deleted with shutil.rmtree and its own worktrees/ID entry (the one whose
gitdir names this tree, never a bare `git worktree prune`) is dropped. A tree that is already
gone only has that entry dropped. No marker, no resume: a dirty or damaged tree refuses exactly as before. If step 4 fails after the worktree is gone, the record is still archived (a record whose
tree is gone only refuses everything) and the failure names the command that finishes the job.

Queued work is looked for in this session and in every sibling session directory (STATE_ROOT/sessions/*) when the state
directory is one of them; the lane's lease is shared by every session and covers work that has started."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lane_commands  # noqa: E402
import lane_deps  # noqa: E402
import lane_lease  # noqa: E402
import lane_steps  # noqa: E402
import lanes  # noqa: E402

MARKER = "removing.json"
MARKER_SCHEMA = "tp-lane-removing:v1"
PROGRESS_AFTER = 5  # seconds before a removal says it is still going; a quick one prints nothing


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


def _marker(common, name, worktree):
    """The removal marker of lane `name`, or None. It is used only when it names this lane's recorded worktree and says the
    tree was clean when the removal began; anything else refuses, and deleting the file makes the removal a normal one."""
    shown = lanes.lane_path(common, name).parent / MARKER
    text = lanes.read_lane_file(common, name, MARKER)
    if text is None:
        return None
    try:
        marker = json.loads(text)
    except ValueError:
        marker = None
    if (not isinstance(marker, dict) or marker.get("schema") != MARKER_SCHEMA or marker.get("lane") != name
            or marker.get("worktree") != worktree or marker.get("clean") is not True):
        raise ValueError(f"lane {name}: the removal marker {shown} is not one for this lane's recorded worktree {worktree}; it is not used. "
                         f"Delete it to remove the lane the normal way (nothing is resumed), or retire the lane by hand (README.md, Lane recovery)")
    return marker


def _write_marker(common, name, worktree):
    text = json.dumps({"schema": MARKER_SCHEMA, "lane": name, "worktree": worktree, "clean": True, "started": _stamp()}, indent=2) + "\n"
    lanes.write_lane_file(common, name, MARKER, text)


def _admin_entry(common, worktree):
    """The one <common>/worktrees/ID directory whose gitdir file names `worktree`'s .git, or None."""
    base, want = os.path.join(common, "worktrees"), os.path.join(worktree, ".git")
    try:
        idents = sorted(os.listdir(base))
    except FileNotFoundError:
        return None
    found = []
    for ident in idents:
        entry = os.path.join(base, ident)
        try:
            if os.path.islink(entry) or not os.path.isdir(entry):
                continue
            with open(os.path.join(entry, "gitdir")) as handle:
                named = handle.readline().strip()
        except OSError:
            continue
        if named and os.path.realpath(named) == want:
            found.append(entry)
    if len(found) > 1:
        raise ValueError(f"{', '.join(found)} all name {want}; none is dropped")
    return found[0] if found else None


def _check_entry(common, entry, worktree):
    """Refuse, before anything is deleted, when the registration `entry` of `worktree` is not one this removal may drop: it is
    not a worktree entry of the repository, or git has it locked (the lock is left, as git leaves it)."""
    if os.path.dirname(entry) != os.path.join(common, "worktrees"):
        raise ValueError(f"{entry} is not a worktree entry of the repository at {common}")
    if os.path.lexists(os.path.join(entry, "locked")):
        raise ValueError(f"git has {worktree} locked; unlock it with `git -C {common} worktree unlock {worktree}`, then remove again")


def _drop_entry(common, entry, worktree):
    """Delete the registration `entry` of a worktree whose tree is gone."""
    _check_entry(common, entry, worktree)
    try:
        shutil.rmtree(entry)
    except OSError as error:
        raise ValueError(f"cannot delete the worktree entry {entry} ({error.__class__.__name__}: {error})")


def _on_branch(cwd, branch, *git_dir):
    """True when HEAD of the worktree at `cwd` (or of the registration `git_dir`, for a tree whose .git link is gone) is refs/heads/`branch`."""
    _, head = lanes._git([*git_dir, "symbolic-ref", "-q", "HEAD"], cwd, ok=(0, 1))
    return head.strip() == f"refs/heads/{branch}"


def _status_entries(cwd, *git_dir):
    """The `git status` entries of the tree at `cwd` (or of the registration `git_dir`, for a tree whose .git link is gone), with
    the ignore rules of the branch tip. Git reads them from the tree's own tracked .gitignore files, and the interrupted removal
    may have deleted some: an ignored node_modules would then show up as untracked. So the deleted .gitignore files (" D", at any
    depth) are put back from HEAD first, and the tree is looked at again. Nothing else is restored here."""
    status = [*git_dir, "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all"]
    entries = lane_commands._status_entries(lanes._git(status, cwd)[1])
    deleted = [f":(literal){paths[0]}" for code, paths in entries if code == " D" and os.path.basename(paths[0]) == ".gitignore"]
    if not deleted:
        return entries
    lanes._git([*git_dir, "restore", "--worktree", "--source=HEAD", "--", *deleted], cwd)
    return lane_commands._status_entries(lanes._git(status, cwd)[1])


def _check_leftovers(record, entry, common):
    """Refuse, deleting nothing, unless the tree without a .git link holds only what `git worktree remove` would have deleted:
    it is still on the recorded branch, and git (told the registration `entry` and the tree explicitly) shows no path that is not
    tracked at the branch tip and not ignored, and no tracked path changed. Files that the interrupted removal deleted (" D")
    are nothing to lose. The marker was written before the interruption; whatever was put here since is somebody's."""
    name, worktree, branch = record["name"], record["worktree"], record["branch"]
    where = ["--git-dir", entry, "--work-tree", worktree]
    try:
        on_branch = _on_branch(worktree, branch, *where)
        entries = _status_entries(worktree, *where)
    except ValueError as error:
        raise ValueError(f"lane {name}: cannot read the state of what is left of {worktree} ({error}); nothing was deleted")
    if not on_branch:
        raise ValueError(f"lane {name}: what is left of {worktree} is no longer on {branch}, the branch of the interrupted removal; "
                         f"nothing was deleted. Remove the marker {lanes.lane_path(common, name).parent / MARKER} and retire the lane by hand "
                         f"(README.md, Lane recovery)")
    others = [paths[0] for code, paths in entries if code != " D"]
    if others:
        shown = ", ".join(others[:5]) + (f" and {len(others) - 5} more" if len(others) > 5 else "")
        raise ValueError(f"lane {name}: what is left of {worktree} has {len(others)} path(s) that are not tracked at {branch} and not ignored, "
                         f"or are changed ({shown}); nothing was deleted. The tree has no .git link, so git cannot set them aside: move them out "
                         f"of {worktree} (or delete them), then remove again")


def _resume(record, repo, common):
    """Prepare what an interrupted removal left (module text). Returns True when the tree is whole enough for `git worktree
    remove` to do the rest, False when nothing is left of it (leftovers deleted, entry dropped)."""
    name, worktree = record["name"], record["worktree"]

    def refuse(text):
        raise ValueError(f"lane {name}: {text}")

    if worktree == repo or lanes._inside(repo, worktree) or lanes._inside(common, worktree):
        refuse(f"worktree {worktree} is, or holds, the repository's own tree; it is not removed")
    entry = _admin_entry(common, worktree)
    if not os.path.lexists(worktree):
        if entry:
            _drop_entry(common, entry, worktree)
        return False
    if os.path.realpath(worktree) != worktree or not os.path.isdir(worktree):
        refuse(f"worktree {worktree} is not the recorded directory (a link or a file); it is left in place")
    if os.path.lexists(os.path.join(worktree, ".git")):
        try:
            lanes.worktree_identity(common, worktree)
        except ValueError as error:
            refuse(f"worktree {worktree} is not the worktree git registered ({error}); it is left in place")
        try:
            on_branch = _on_branch(worktree, record["branch"])
        except ValueError as error:
            refuse(f"cannot read the branch of worktree {worktree} ({error})")
        if not on_branch:
            refuse(f"worktree {worktree} is no longer on {record['branch']}, the branch of the interrupted removal; it is left as it is. "
                   f"Check {record['branch']} out again, or remove the marker {lanes.lane_path(common, name).parent / MARKER} and retire the lane by hand "
                   f"(README.md, Lane recovery)")
        try:
            entries = _status_entries(worktree)
        except ValueError as error:
            refuse(f"cannot read the state of worktree {worktree} ({error})")
        others = [paths for code, paths in entries if code != " D"]
        if others:
            raise ValueError(f"lane {name}'s tree has {len(others)} uncommitted path(s) besides the files an interrupted removal deleted. "
                             f"Set them aside with `git -C {worktree} stash push -u -m \"lane {name}: before remove\"` (the stash keeps them), "
                             f"then remove again")
        if entries:  # only tracked files that the interrupted removal deleted: put them back, so git's own clean check passes
            try:
                lanes._git(["restore", "--worktree", "--source=HEAD", "--", "."], worktree)
            except ValueError as error:
                refuse(f"git could not restore the files an interrupted removal deleted in {worktree} ({error}); the tree is left as it is")
        return True
    if entry is None:
        refuse(f"worktree {worktree} has no .git link and git has no entry for it, so it is not one this removal started on; it is left in place")
    _check_entry(common, entry, worktree)
    _check_leftovers(record, entry, common)
    try:
        shutil.rmtree(worktree)
    except OSError as error:
        raise ValueError(f"lane {name}: cannot delete what is left of {worktree} ({error.__class__.__name__}: {error}); run the removal again")
    _drop_entry(common, entry, worktree)
    return False


def _unlink_deps(repo, worktree):
    """os.unlink the lane's shared node_modules links before anything is deleted (lane_deps; a link is never followed)."""
    if os.path.isdir(worktree) and not os.path.islink(worktree):
        try:
            lane_deps.unlink_shared(repo, worktree)
        except OSError as error:
            raise ValueError(f"cannot unlink the shared node_modules in {worktree} ({error.__class__.__name__}); nothing was deleted")


def _again(repo, name, abandon):
    return f"tp-agent lane remove {name}{' --abandon' if abandon else ''} --repo {repo}"


def _remove_tree(record, repo, abandon):
    """`git worktree remove` with the long timeout (never --force). A timeout or an interrupt may leave the tree half deleted;
    the marker is already written, so running the command again finishes it."""
    name, worktree = record["name"], record["worktree"]
    interrupted = (f"removing lane {name}'s worktree {worktree} was interrupted and may be partly done; run "
                   f"`{_again(repo, name, abandon)}` again to finish it")
    timer = threading.Timer(PROGRESS_AFTER, lambda: print(f"tp-agent: still removing lane {name}'s worktree {worktree} (minutes, with node_modules)",
                                                          file=sys.stderr, flush=True))
    timer.daemon = True
    timer.start()
    try:
        lanes._git(["worktree", "remove", worktree], repo, timeout=lanes.REMOVE_TIMEOUT)  # never --force
    except lanes.GitTimeout as error:
        raise ValueError(f"{interrupted} ({error})")
    except ValueError as error:
        raise ValueError(_tree_refusal(record, repo, error))
    except KeyboardInterrupt:
        print(f"tp-agent: {interrupted}", file=sys.stderr)
        raise
    finally:
        timer.cancel()


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
        worktree, branch = record["worktree"], record["branch"]
        resuming = _marker(common, name, worktree) is not None
        # a full check also proves the worktree is a linked one git registered: never the main tree; a resumed removal has only _resume's
        tip = lanes.lane_reality(record, history_only=resuming)["tip"]
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
        if resuming:
            print(f"tp-agent: lane {name}: an earlier removal did not finish; resuming it", file=sys.stderr)
            _unlink_deps(repo, worktree)
            whole = _resume(record, repo, common)
        else:
            whole = True
            try:
                clean = lane_steps._dirty(worktree) == 0
            except ValueError as error:  # without the marker an interrupted removal could not be resumed: do not start one
                raise ValueError(f"lane {name}: cannot tell whether {worktree} is clean ({error}); nothing was removed")
            if clean:  # git's own clean check follows; a tree that is not clean gets no marker and is refused by git as before
                try:
                    _write_marker(common, name, worktree)
                except ValueError as error:
                    raise ValueError(f"lane {name}: cannot write the removal marker ({error}); nothing was removed")
                _unlink_deps(repo, worktree)
        if whole:
            _remove_tree(record, repo, abandon)
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
